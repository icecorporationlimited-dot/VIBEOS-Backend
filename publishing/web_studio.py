"""Browser studio API: upload photos to the hosted buffer + control Autopilot.

All routes need a valid web session. State-changing routes also need:
  * Origin == our public origin,
  * X-CSRF-Token header == the CSRF cookie == the session's CSRF token.

Every read/write goes through the verified Principal (owner_id scoping), so one
web user can never see, change or delete another user's photos, jobs or settings.

Photos are re-encoded to JPEG on the way in. That guarantees a real image, drops
EXIF (GPS location, camera serials), and makes identical photos hash identically
so the buffer's duplicate check works.
"""
import hmac
import io
import json
import logging
import uuid
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as email_policy

from content_buffer import MediaTooLarge, QuotaExceeded
from . import service as svc
from .web_auth import SESSION_COOKIE
from .web_routes import CSRF_COOKIE, _cookie_value

logger = logging.getLogger(__name__)

MAX_FILES_PER_UPLOAD = 10
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_UPLOAD_BODY = 45 * 1024 * 1024
MAX_PIXELS = 50_000_000
MAX_LIST = 200

# Settings the browser may change (everything else is rejected, not ignored).
AUTOPILOT_FIELDS = ("enabled", "paused", "daily_limit", "slots",
                    "quiet_start", "quiet_end", "min_gap_minutes", "timezone",
                    "auto_replies", "auto_comments", "trend_stories", "formats")

REJECT_LABELS = {
    "duplicate_of_used": "Already posted a similar photo",
    "duplicate_in_batch": "Duplicate of another upload",
    "discarded_by_user": "Discarded",
}


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _json(status, obj):
    return status, json.dumps(obj), {}


def _err(status, message):
    return _json(status, {"error": message})


def to_clean_jpeg(raw):
    """Decode, rotate per EXIF, strip metadata, re-encode. ValueError if not an image."""
    from PIL import Image, ImageOps
    try:
        img = Image.open(io.BytesIO(raw))
        if img.width * img.height > MAX_PIXELS:
            raise ValueError("Image is too large.")
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, "JPEG", quality=92, optimize=True)
        return out.getvalue()
    except ValueError:
        raise
    except Exception:
        raise ValueError("Not a valid image.") from None


def _parse_files(content_type, raw):
    """Return [(filename, bytes)] for every multipart part named 'files'."""
    msg = BytesParser(policy=email_policy).parsebytes(
        ("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + raw)
    if not msg.is_multipart():
        raise ValueError("Expected a multipart photo upload.")
    files = []
    for part in msg.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        if part.get_param("name", header="content-disposition") != "files":
            continue
        name = (part.get_filename() or "photo").replace("\\", "/").split("/")[-1][:120]
        files.append((name, part.get_payload(decode=True) or b""))
    return files


class WebStudio:
    def __init__(self, cfg, sessions, buffer, autopilot, accounts=None,
                 service=None, ingest=None, posting_engine=False, planner=None, story_db=None):
        self.story_db = story_db        # main Mongo db holding the story library (se_* collections) or None
        self.cfg, self.sessions, self.buffer = cfg, sessions, buffer
        self.autopilot, self.accounts = autopilot, accounts
        self.service, self.ingest = service, ingest
        self.planner = planner          # context_engine.ContextPlanner or None
        # False until the worker publishes web-owned jobs; the UI tells the truth about it.
        self.posting_engine = bool(posting_engine)
        self.origin = cfg.public_base_url.rstrip("/")

    # ---- auth ---------------------------------------------------------------
    def _principal(self, cookie_header):
        return self.sessions.resolve(_cookie_value(cookie_header, SESSION_COOKIE))

    def _authorize_write(self, cookie_header, headers):
        """-> (principal, None) or (None, error response)."""
        token = _cookie_value(cookie_header, SESSION_COOKIE)
        principal = self.sessions.resolve(token)
        if principal is None:
            return None, _err(401, "Please sign in again.")
        csrf_cookie = _cookie_value(cookie_header, CSRF_COOKIE)
        csrf_header = headers.get("X-CSRF-Token", "")
        ok = (headers.get("Origin", "") == self.origin and csrf_cookie and csrf_header
              and hmac.compare_digest(csrf_cookie, csrf_header)
              and self.sessions.validate_csrf(token, csrf_header))
        if not ok:
            return None, _err(403, "Request rejected. Refresh the page and try again.")
        return principal, None

    # ---- state --------------------------------------------------------------
    def state(self, cookie_header):
        principal = self._principal(cookie_header)
        if principal is None:
            return _err(401, "Please sign in.")
        settings = self.autopilot.get(principal)
        active = bool(settings["enabled"] and settings["full_auto"] and not settings["paused"])
        jobs = self.service.jobs_for(principal, MAX_LIST) if self.service else []
        by_asset = {}
        for j in jobs:                                   # newest first
            for aid in j.get("asset_ids") or []:
                by_asset.setdefault(aid, j)

        media, counts = [], {"waiting": 0, "processing": 0, "ready": 0,
                             "scheduled": 0, "posted": 0, "skipped": 0}
        for a in reversed(self.buffer.list_assets(principal)[-MAX_LIST:]):
            if a.get("kind", "photo") != "photo" or a.get("source") in ("trend_story", "reel_builder"):
                continue
            state, label, when = self._label(a, by_asset.get(a["_id"]), active)
            counts[state if state in counts else "skipped"] += 1
            media.append({
                "id": a["_id"], "name": a.get("filename") or "photo",
                "size": a.get("size_bytes") or 0, "state": state, "label": label,
                "when": when, "created_at": _iso(a.get("created_at")),
                "thumb": "/web/media/%s/%s" % (a["_id"], "post" if a["status"] == "ready" else "original"),
                "caption": ((a.get("creative") or {}).get("caption") or "")[:300],
            })

        pending = [j for j in jobs if j["status"] in svc.PENDING]
        next_at = min((j.get("scheduled_at") or j.get("next_attempt_at") for j in pending
                       if j.get("scheduled_at") or j.get("next_attempt_at")),
                      default=None, key=lambda d: _iso(d))
        account = None
        if self.accounts is not None:
            try:
                account = self.accounts.get_for(principal)
            except Exception:
                account = None
        return _json(200, {
            "media": media,
            "counts": counts,
            "autopilot": {
                "active": active,
                "enabled": bool(settings["enabled"]), "paused": bool(settings["paused"]),
                "daily_limit": settings["daily_limit"], "slots": settings["slots"],
                "quiet_start": settings["quiet_start"], "quiet_end": settings["quiet_end"],
                "min_gap_minutes": settings["min_gap_minutes"],
                "timezone": settings["timezone"],
                "auto_replies": bool(settings.get("auto_replies")),
                "auto_comments": bool(settings.get("auto_comments")),
                "trend_stories": settings.get("trend_stories") == "auto",
                "reels": "reel" in settings["formats"],
                "next_post_at": _iso(next_at),
            },
            "account": {
                "connected": bool(account and account.get("status") == "active"),
                "username": str((account or {}).get("username") or ""),
                "status": str((account or {}).get("status") or ""),
            },
            "recent": [{
                "format": j["format"], "status": j["status"],
                "permalink": j.get("permalink"), "error": (j.get("last_error") or "")[:160],
                "at": _iso(j.get("published_at") or j.get("scheduled_at") or j.get("created_at")),
            } for j in jobs[:10]],
            "posting_engine": self.posting_engine,
        })

    @staticmethod
    def _label(asset, job, active):
        """-> (state, human label, ISO time or None) for one library photo."""
        status = asset["status"]
        if job is not None and job["status"] == svc.PUBLISHED:
            return "posted", "Posted", _iso(job.get("published_at"))
        if job is not None and job["status"] in svc.PENDING:
            if job["status"] in (svc.HELD, svc.NEEDS_ATTENTION, svc.NEEDS_REAUTH):
                return "scheduled", "Needs attention", None
            return "scheduled", "Scheduled", _iso(job.get("scheduled_at") or job.get("next_attempt_at"))
        if status == "rejected":
            reason = str(asset.get("reject_reason") or "")
            if reason.startswith("quality:"):
                return "skipped", "Skipped: photo quality", None
            return "skipped", REJECT_LABELS.get(reason, "Skipped"), None
        if status == "ready":
            return "ready", "Ready to post", None
        note = asset.get("web_note")
        if note == "low_score":
            return "skipped", "Skipped: low AI score", None
        if note == "failed":
            return "skipped", "Couldn't process", None
        if active:
            return "processing", "Preparing…", None
        return "waiting", "In buffer · Autopilot off", None

    # ---- occasions (context engine) -------------------------------------------
    def occasions(self, cookie_header):
        principal = self._principal(cookie_header)
        if principal is None:
            return _err(401, "Please sign in.")
        if self.planner is None:
            return _json(200, {"available": False})
        jobs = self.service.jobs_for(principal, 300) if self.service else []
        try:
            data = self.planner.overview(principal, jobs)
        except Exception:
            logger.exception("Occasion overview failed")
            return _err(500, "Could not load occasions.")
        data["available"] = True
        return _json(200, data)

    def set_occasions(self, cookie_header, headers, body):
        principal, error = self._authorize_write(cookie_header, headers)
        if error:
            return error
        try:
            data = json.loads(body or b"{}")
            from context_engine.prefs import clean_prefs
            fresh = clean_prefs(data)
            current = (self.autopilot.get(principal).get("occasions") or {})
            self.autopilot.update(principal, occasions={**current, **fresh})
        except ValueError as exc:
            return _err(400, str(exc))
        return self.occasions(cookie_header)

    # ---- upload -------------------------------------------------------------
    def upload(self, cookie_header, headers, raw):
        principal, error = self._authorize_write(cookie_header, headers)
        if error:
            return error
        try:
            files = _parse_files(headers.get("Content-Type", ""), raw)
        except ValueError as exc:
            return _err(400, str(exc))
        except Exception:
            return _err(400, "Could not read the upload.")
        if not files:
            return _err(400, "Choose at least one photo.")
        if len(files) > MAX_FILES_PER_UPLOAD:
            return _err(413, "Upload at most %d photos at a time." % MAX_FILES_PER_UPLOAD)

        batch_id = "web-" + uuid.uuid4().hex
        added = duplicates = invalid = too_large = quota = 0
        for name, data in files:
            if not data:
                invalid += 1
                continue
            if len(data) > MAX_FILE_BYTES:
                too_large += 1
                continue
            try:
                clean = to_clean_jpeg(data)
            except ValueError:
                invalid += 1
                continue
            try:
                res = self.buffer.add_asset(principal, clean, "photo", batch_id,
                                            filename=name, source="web")
            except QuotaExceeded:
                quota += 1
                continue
            except MediaTooLarge:
                too_large += 1
                continue
            except Exception:
                logger.exception("Web upload store failed")
                invalid += 1
                continue
            if res["duplicate"]:
                duplicates += 1
            else:
                added += 1
        if added and self.ingest is not None:
            self.ingest.submit(principal, batch_id)      # no-op work if autopilot is off
        if not added and not duplicates:
            msg = "No photos were added."
            if quota:
                msg = "Daily upload limit reached. Try again tomorrow."
            elif too_large:
                msg = "Photos must be under 10 MB."
            elif invalid:
                msg = "Choose valid JPG, PNG or WEBP photos."
            return _err(400, msg)
        return _json(201, {"ok": True, "added": added, "duplicates": duplicates,
                           "invalid": invalid, "too_large": too_large,
                           "quota_blocked": quota})

    # ---- delete -------------------------------------------------------------
    def delete_media(self, cookie_header, headers, body):
        principal, error = self._authorize_write(cookie_header, headers)
        if error:
            return error
        try:
            asset_id = str(json.loads(body or b"{}").get("id") or "")
        except (ValueError, AttributeError):
            return _err(400, "Invalid request.")
        if not asset_id:
            return _err(400, "Missing photo id.")
        if self.service is not None:                      # never leave a job pointing at a deleted photo
            for job in self.service.jobs_for(principal, 500):
                if asset_id in (job.get("asset_ids") or []) and job["status"] in svc.PENDING:
                    self.service.cancel(job["_id"], principal)
        if not self.buffer.delete_asset(principal, asset_id):
            return _err(404, "Photo not found.")
        return _json(200, {"ok": True})

    # ---- story library -> publishing queue (story_engine step 11) -----------------
    def queue_story(self, cookie_header, headers, body):
        principal, error = self._authorize_write(cookie_header, headers)
        if error:
            return error
        if self.story_db is None or self.service is None:
            return _err(503, "Story publishing is not available.")
        try:
            data = json.loads(body or b"{}")
            asset_id = str(data.get("story_asset_id") or "")
            if not asset_id.isalnum() or len(asset_id) > 64:
                return _err(400, "Invalid story.")
            when = None
            if data.get("scheduled_at"):
                when = datetime.fromisoformat(str(data["scheduled_at"]).replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError, TypeError):
            return _err(400, "Invalid request.")
        from story_engine.publishing_integration import StoryPublishError, queue_story
        try:
            out = queue_story(self.story_db, self.buffer, self.service, principal, asset_id, when)
        except StoryPublishError as exc:
            return _err(409, str(exc))
        except Exception:
            logger.exception("Story queue failed")
            return _err(500, "Could not queue this story.")
        return _json(201 if not out["duplicate"] else 200, {"ok": True, **out})

    # ---- autopilot ----------------------------------------------------------
    def set_autopilot(self, cookie_header, headers, body):
        principal, error = self._authorize_write(cookie_header, headers)
        if error:
            return error
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return _err(400, "Invalid request.")
        if not isinstance(data, dict) or not data:
            return _err(400, "Nothing to change.")
        unknown = [k for k in data if k not in AUTOPILOT_FIELDS]
        if unknown:
            return _err(400, "Unknown setting: " + ", ".join(sorted(map(str, unknown))))
        toggle = data.pop("enabled", None)
        try:
            if data:
                self.autopilot.update(principal, **data)
            if toggle is not None:
                self.autopilot.set_web_autopilot(principal, bool(toggle))
        except ValueError as exc:
            return _err(400, str(exc))
        if toggle and self.ingest is not None:            # photos uploaded while it was off start now
            self.ingest.submit_pending(principal)
        return self.state(cookie_header)
