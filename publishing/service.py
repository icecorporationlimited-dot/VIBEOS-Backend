r"""
publishing/service.py — durable publish jobs with safe recovery.

Job lifecycle
    queued -> running -> published
                     \-> retry          (temporary failure: rate limit, 5xx, network)
                     \-> held           (Phase 3 policy said no: blocked topic, format, duplicate)
    queued/retry/held -> cancelled      (user /cancel)
                     \-> failed         (permanent: bad media, rejected by Instagram)
                     \-> needs_reauth   (token dead; user must /connect)
                     \-> needs_attention(outcome unknown: never auto-repost)

Duplicate protection (the whole point of this module):
  * `idempotency_key` = account + format + asset ids, unique index. Asking to
    publish the same thing twice returns the existing job.
  * The Instagram container id is saved BEFORE anything else happens, so a crash
    or restart resumes the same container instead of creating a second post.
  * `publish_started_at` is saved BEFORE media_publish. If we find that marker
    but no media id (response lost), we ask Instagram about the container: if it
    is already PUBLISHED we do NOT publish again, we flag it for the user.
  * A lease prevents two workers from running one job at once.

Phase 3 additions
  * `gate` (set by the PolicyEngine) is asked before a job is claimed AND again
    right before media_publish, so /pause or a policy change always wins.
  * Jobs carry `scheduled_at`, `source` (manual|scheduled) and `content_hash`.
"""
import hashlib
import io
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from PIL import Image
try:
    from pymongo.errors import DuplicateKeyError
except Exception:  # pymongo missing/stubbed (offline tests)
    class DuplicateKeyError(Exception):
        pass

from . import crypto
from .accounts import NotConnected, ReauthRequired
from .graph import GraphError
from .identity import Principal, owner_filter

logger = logging.getLogger(__name__)

QUEUED, RUNNING, RETRY, PUBLISHED = "queued", "running", "retry", "published"
FAILED, NEEDS_REAUTH, NEEDS_ATTENTION = "failed", "needs_reauth", "needs_attention"
HELD, CANCELLED = "held", "cancelled"
FINAL = {PUBLISHED, FAILED, CANCELLED}
PENDING = (QUEUED, RETRY, RUNNING, HELD, NEEDS_REAUTH, NEEDS_ATTENTION)

FORMATS = ("photo", "story", "carousel", "reel")
VARIANT_FOR = {"photo": "post", "story": "story", "carousel": "post", "reel": "reel"}
IG_MAX_REEL_BYTES = 300 * 1024 * 1024
REEL_MIN_SECONDS, REEL_MAX_SECONDS = 3, 90
LEASE_SECONDS = 600
MAX_ATTEMPTS = 5
PUBLISH_SETTLE_SECONDS = 120
BACKOFF = (120, 300, 900, 3600, 7200)   # seconds, per attempt
IG_MAX_JPEG_BYTES = 8 * 1024 * 1024
CAPTION_LIMIT, HASHTAG_LIMIT = 2200, 30


class PublishError(Exception):
    """Permanent problem found before/while publishing (user-fixable)."""


class Blocked(Exception):
    """The policy gate said not now (raised inside a run, handled by run())."""

    def __init__(self, decision):
        super().__init__(getattr(decision, "reason", ""))
        self.decision = decision


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _owned_query(principal, query):
    """Combine a resource selector with the verified owner's filter."""
    return {"$and": [query, owner_filter(principal)]}


def build_caption(creative) -> str:
    caption = (creative.get("caption") or "").strip()
    tags = []
    for t in creative.get("hashtags") or []:
        t = str(t).strip().lstrip("#").replace(" ", "")
        if t and f"#{t}" not in tags:
            tags.append(f"#{t}")
    tags = tags[:HASHTAG_LIMIT]
    text = caption + ("\n\n" + " ".join(tags) if tags else "")
    return text[:CAPTION_LIMIT]


def preflight_image(data: bytes, fmt: str):
    """Reject media Instagram would refuse, before we spend an API call."""
    if len(data) > IG_MAX_JPEG_BYTES:
        raise PublishError("Image is larger than 8 MB.")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise PublishError("Image file is unreadable.") from None
    if img.format != "JPEG":
        raise PublishError("Instagram needs JPEG images.")
    w, h = img.size
    ratio = w / h
    if fmt in ("photo", "carousel") and not (0.8 <= ratio <= 1.91):
        raise PublishError("Feed images must be between 4:5 and 1.91:1.")
    if fmt == "story" and not (0.5 <= ratio <= 0.6):
        raise PublishError("Stories should be 9:16.")
    if min(w, h) < 320:
        raise PublishError("Image is too small.")


def preflight_reel(data: bytes, asset: dict):
    """Reject a Reel Instagram would refuse. Uses the metadata saved at render time."""
    if len(data) > IG_MAX_REEL_BYTES:
        raise PublishError("Reel video is larger than 300 MB.")
    meta = (asset.get("analysis") or {}).get("video") or {}
    duration = meta.get("duration_seconds")
    if duration is None or not REEL_MIN_SECONDS <= float(duration) <= REEL_MAX_SECONDS:
        raise PublishError(f"Reels must be {REEL_MIN_SECONDS}-{REEL_MAX_SECONDS} seconds long.")
    if int(meta.get("height") or 0) <= int(meta.get("width") or 0):
        raise PublishError("Reels should be vertical (9:16).")


class PublishService:
    def __init__(self, jobs, events, buffer, accounts, graph, cfg,
                 sleep=time.sleep, clock=_now):
        self.jobs, self.events, self.buffer = jobs, events, buffer
        self.accounts, self.graph, self.cfg = accounts, graph, cfg
        self.sleep, self.clock = sleep, clock
        # Phase 3: callable(job, final=False) -> Decision | None (see policy.py)
        self.gate = None

    def ensure_indexes(self):
        self.jobs.create_index("idempotency_key", unique=True)
        self.jobs.create_index([("telegram_id", 1), ("created_at", -1)])
        self.jobs.create_index([("owner_id", 1), ("created_at", -1)])
        self.jobs.create_index([("status", 1), ("next_attempt_at", 1)])
        self.jobs.create_index([("ig_user_id", 1), ("content_hash", 1)])
        self.jobs.create_index([("ig_user_id", 1), ("status", 1), ("published_at", -1)])
        self.events.create_index([("job_id", 1), ("at", 1)])

    # ---- helpers ---------------------------------------------------------
    @staticmethod
    def _principal(identity):
        # Web callers must pass a Principal created by the validated session
        # adapter; bare IDs remain the Telegram compatibility interface.
        return identity if isinstance(identity, Principal) else Principal.from_telegram(identity)

    @staticmethod
    def _telegram_worker_eligible(job):
        """Fail closed unless the job's owner and Telegram identity agree.

        Legacy rows without owner_id remain eligible when they have a valid
        Telegram ID. Namespaced rows must match that exact ID; a generic
        'tg:' prefix is not enough to authorize execution.
        """
        telegram_id = job.get("telegram_id")
        if telegram_id is None or str(telegram_id).strip() == "":
            return False
        try:
            expected_owner = Principal.from_telegram(telegram_id).owner_id
        except (TypeError, ValueError):
            return False
        owner_id = job.get("owner_id")
        return owner_id is None or owner_id == expected_owner

    def media_url(self, blob_id, ext="jpg") -> str:
        token = crypto.sign(self.cfg.signing_secret, {"b": blob_id},
                            self.cfg.media_url_ttl)
        return f"{self.cfg.public_base_url.rstrip('/')}/media/{token}.{ext}"

    def _log(self, job_id, step, ok, detail=None):
        self.events.insert_one({"_id": uuid.uuid4().hex, "job_id": job_id,
                                "step": step, "ok": ok, "detail": detail or {},
                                "at": self.clock()})

    def _set(self, job_id, **fields):
        fields["updated_at"] = self.clock()
        self.jobs.update_one({"_id": job_id}, {"$set": fields})

    # ---- creating jobs ---------------------------------------------------
    def enqueue(self, telegram_id, asset_ids, fmt, scheduled_at=None,
                source="manual", revive=False) -> dict:
        """Backward-compatible Telegram enqueue entry point."""
        return self.enqueue_for(
            Principal.from_telegram(telegram_id), asset_ids, fmt,
            scheduled_at=scheduled_at, source=source, revive=revive,
        )

    def enqueue_for(self, principal, asset_ids, fmt, scheduled_at=None,
                    source="manual", revive=False) -> dict:
        """Create a job for one verified principal.

        This is an owner-aware service primitive, not a switch that enables web
        publishing. The current background worker deliberately remains
        Telegram-only until browser notifications and the complete web publish
        lifecycle are integrated and tested.

        scheduled_at  run no earlier than this (tz-aware UTC). None = ASAP.
        source        "manual" or "scheduled".
        revive        re-queue a previously cancelled identical job.
        """
        if not isinstance(principal, Principal):
            raise TypeError("A verified Principal is required.")
        if fmt not in FORMATS:
            raise PublishError(f"Unknown format: {fmt}")
        asset_ids = list(asset_ids)
        if fmt == "carousel":
            if not 2 <= len(asset_ids) <= 10:
                raise PublishError("A carousel needs 2 to 10 photos.")
        elif len(asset_ids) != 1:
            raise PublishError("Exactly one photo is needed.")
        if len(set(asset_ids)) != len(asset_ids):
            raise PublishError("The same photo was selected twice.")

        acct = self.accounts.get_for(principal)
        if not acct or acct.get("status") == "disconnected":
            raise NotConnected("No Instagram account connected. Use /connect.")

        digests = []
        for aid in asset_ids:
            asset = self.buffer.assets.find_one(
                _owned_query(principal, {"_id": aid})
            )
            if not asset:
                raise PublishError("Photo not found in your buffer.")
            if asset["status"] != "ready":
                raise PublishError("Only publish-ready photos can be posted.")
            digests.append(asset.get("sha256") or aid)

        # Include the tenant in the key: deduplication must not turn into a
        # cross-owner lookup if account IDs or asset identifiers are reused.
        key = f"{principal.owner_id}:{acct['ig_user_id']}:{fmt}:" + ",".join(sorted(asset_ids))
        content_hash = hashlib.sha256(",".join(sorted(digests)).encode()).hexdigest()
        now = self.clock()
        when = _aware(scheduled_at) if scheduled_at else now
        job = {
            "_id": uuid.uuid4().hex, "idempotency_key": key,
            "owner_id": principal.owner_id,
            "ig_user_id": acct["ig_user_id"],
            "format": fmt, "asset_ids": asset_ids, "status": QUEUED,
            "attempts": 0, "container_id": None, "child_ids": [],
            "media_id": None, "permalink": None, "publish_started_at": None,
            "last_error": None, "next_attempt_at": when, "lease_until": None,
            "scheduled_at": when if scheduled_at else None, "source": source,
            "content_hash": content_hash, "hold_reason": None, "notified": None,
            "created_at": now, "updated_at": now,
        }
        if principal.kind == "telegram":
            # Keep legacy fields for Telegram consumers while the worker migrates.
            job["telegram_id"] = principal.subject
        try:
            self.jobs.insert_one(job)
        except DuplicateKeyError:
            existing = self.jobs.find_one(_owned_query(
                principal, {"idempotency_key": key}
            ))
            if not existing:
                raise
            if revive and existing["status"] == CANCELLED:
                self.jobs.update_one(
                    _owned_query(principal, {"_id": existing["_id"], "status": CANCELLED}),
                    {"$set": {"status": QUEUED, "attempts": 0, "next_attempt_at": when,
                              "scheduled_at": when if scheduled_at else None,
                              "source": source, "hold_reason": None, "notified": None,
                              "last_error": None, "updated_at": now}})
                existing = self.jobs.find_one(_owned_query(
                    principal, {"_id": existing["_id"]}
                ))
                existing["duplicate"] = False
                return existing
            existing["duplicate"] = True
            return existing
        job["duplicate"] = False
        return job

    def retry(self, job_id, telegram_id):
        """Retry a job only when it belongs to the supplied verified principal."""
        principal = self._principal(telegram_id)
        res = self.jobs.update_one(
            _owned_query(principal, {
                "_id": job_id, "status": {"$in": [RETRY, FAILED, NEEDS_REAUTH]}}),
            {"$set": {"status": QUEUED, "next_attempt_at": self.clock(),
                      "attempts": 0, "updated_at": self.clock()}})
        return res.matched_count == 1

    def cancel(self, job_id, telegram_id):
        """Atomically cancel an eligible job owned by this principal."""
        principal = self._principal(telegram_id)
        res = self.jobs.update_one(
            _owned_query(principal, {
                "_id": job_id, "status": {"$in": [QUEUED, RETRY, HELD, NEEDS_REAUTH]}}),
            {"$set": {"status": CANCELLED, "updated_at": self.clock()}})
        return res.matched_count == 1

    def release(self, job_id, telegram_id):
        """Release a held job only for its owner (not a duplicate job)."""
        principal = self._principal(telegram_id)
        res = self.jobs.update_one(
            _owned_query(principal, {"_id": job_id, "status": HELD}),
            {"$set": {"status": QUEUED, "policy_override": True, "hold_reason": None,
                      "notified": None, "next_attempt_at": self.clock(),
                      "updated_at": self.clock()}})
        return res.matched_count == 1

    def queue(self, telegram_id):
        """List pending jobs belonging only to this principal."""
        principal = self._principal(telegram_id)
        rows = [j for j in self.jobs.find(_owned_query(principal, {}))
                if j["status"] in PENDING]
        return sorted(rows, key=lambda j: _aware(j.get("next_attempt_at") or j["created_at"]))

    def find_by_prefix(self, telegram_id, prefix):
        prefix = (prefix or "").strip().lower()
        if not prefix:
            return []
        principal = self._principal(telegram_id)
        return [j for j in self.jobs.find(_owned_query(principal, {}))
                if j["_id"].startswith(prefix)]

    # ---- running jobs ----------------------------------------------------
    def _claim(self, job_id):
        now = self.clock()
        res = self.jobs.update_one(
            {"_id": job_id, "$or": [
                {"status": {"$in": [QUEUED, RETRY]}},
                {"status": RUNNING, "lease_until": {"$lt": now}},
            ]},
            {"$set": {"status": RUNNING, "updated_at": now,
                      "lease_until": now + timedelta(seconds=LEASE_SECONDS)},
             "$inc": {"attempts": 1}})
        return res.matched_count == 1

    def run(self, job_id) -> dict:
        """Run (or resume) one job. Never raises for expected failures."""
        job = self.jobs.find_one({"_id": job_id})
        if job is None:
            return None
        # The current worker resolves accounts and assets through Telegram IDs.
        # Fail closed for web/unknown owners and inconsistent legacy mappings.
        if not self._telegram_worker_eligible(job):
            return job
        if self._claimable(job):
            blocked = self._gate_decision(job)          # before we touch Instagram
            if blocked is not None:
                self._apply_block(job, blocked, consumed=False)
                return self.jobs.find_one({"_id": job_id})
        if not self._claim(job_id):
            return self.jobs.find_one({"_id": job_id})  # done, or another worker has it
        job = self.jobs.find_one({"_id": job_id})
        try:
            self._execute(job)
        except Blocked as blk:                          # policy changed mid-run
            self._apply_block(job, blk.decision, consumed=True)
        except ReauthRequired as exc:
            self._set(job_id, status=NEEDS_REAUTH, last_error=str(exc))
            self._log(job_id, "auth", False, {"error": str(exc)})
        except NotConnected as exc:
            self._set(job_id, status=FAILED, last_error=str(exc))
        except PublishError as exc:
            self._set(job_id, status=FAILED, last_error=str(exc))
            self._log(job_id, "preflight", False, {"error": str(exc)})
        except GraphError as exc:
            self._handle_graph_error(job, exc)
        except Exception as exc:
            logger.exception("Unexpected publish failure for %s", job_id)
            self._schedule_retry(job, f"unexpected: {exc.__class__.__name__}")
        return self.jobs.find_one({"_id": job_id})

    def _claimable(self, job):
        if job["status"] in (QUEUED, RETRY):
            return True
        lease = job.get("lease_until")
        return job["status"] == RUNNING and lease is not None \
            and _aware(lease) < self.clock()

    def _gate_decision(self, job, final=False):
        """None = go ahead; otherwise the Decision that stops this run."""
        if self.gate is None:
            return None
        decision = self.gate(job, final=final)
        return None if decision is None or decision.action == "allow" else decision

    def _apply_block(self, job, decision, consumed):
        """Put a blocked job back in a clean state. `consumed`: undo the attempt
        that _claim() counted, so policy waiting never eats the retry budget."""
        jid, act = job["_id"], decision.action
        fields = {"updated_at": self.clock(), "lease_until": None}
        if act == "hold":
            fields.update(status=HELD, hold_reason=decision.reason)
            self._log(jid, "policy_hold", False, {"reason": decision.reason})
        elif act == "defer":
            fields.update(status=QUEUED, next_attempt_at=decision.until,
                          defer_reason=decision.reason)
            self._log(jid, "policy_defer", True, {"reason": decision.reason,
                                                  "until": decision.until.isoformat()})
        else:  # "skip": leave everything as it was (paused, not due yet)
            if not consumed:
                return
            fields.update(status=QUEUED)
        update = {"$set": fields}
        if consumed:
            update["$inc"] = {"attempts": -1}
        self.jobs.update_one({"_id": jid}, update)

    def _handle_graph_error(self, job, exc):
        self._log(job["_id"], "graph_error", False, exc.summary())
        if exc.auth:
            self.accounts.mark_needs_reauth(job["telegram_id"], f"api_code_{exc.code}")
            self._set(job["_id"], status=NEEDS_REAUTH, last_error=str(exc))
        elif exc.transient:
            self._schedule_retry(job, str(exc))
        else:
            self._set(job["_id"], status=FAILED, last_error=str(exc))

    def _schedule_retry(self, job, message):
        attempts = self.jobs.find_one({"_id": job["_id"]})["attempts"]
        if attempts >= MAX_ATTEMPTS:
            self._set(job["_id"], status=FAILED, last_error=f"gave up: {message}")
            return
        delay = BACKOFF[min(attempts - 1, len(BACKOFF) - 1)]
        self._set(job["_id"], status=RETRY, last_error=message,
                  next_attempt_at=self.clock() + timedelta(seconds=delay))

    def _execute(self, job):
        jid = job["_id"]
        ig_id, token = self.accounts.get_token(job["telegram_id"])
        fmt = job["format"]

        assets = []
        for aid in job["asset_ids"]:
            asset = self.buffer.assets.find_one(
                _owned_query(Principal.from_telegram(job["telegram_id"]), {"_id": aid})
            )
            if not asset or asset["status"] != "ready":
                raise PublishError("Photo is no longer publish-ready.")
            assets.append(asset)
        caption = build_caption(assets[0]["creative"])

        # ---- step 1: container (resume if we already have one) ----
        if not job["container_id"]:
            self._precheck_quota(ig_id, token, jid)
            container = self._create_container(job, assets, caption, ig_id, token)
            self._set(jid, container_id=container)
            job["container_id"] = container
        container = job["container_id"]

        # ---- step 2: wait until Instagram finished processing ----
        status = self._wait_finished(container, token, jid)
        if status == "EXPIRED":
            # Container died (24h). Safe: nothing was published from it.
            self._set(jid, container_id=None, child_ids=[], publish_started_at=None)
            raise GraphError("Container expired; will recreate", transient=True)
        if status == "PUBLISHED":
            if job.get("media_id"):
                return self._finish(job, job["media_id"], token)
            # Published but we lost the id: do NOT publish again.
            self._set(jid, status=NEEDS_ATTENTION,
                      last_error="Instagram says this post is already live, but "
                                 "the confirmation was lost. Check your profile; "
                                 "it was not posted twice.")
            self._log(jid, "publish_unknown", False, {"container": container})
            return
        if status == "ERROR":
            raise PublishError("Instagram could not process this media.")

        # ---- step 3: publish (mark intent first) ----
        started = job.get("publish_started_at")
        if started and not job.get("media_id"):
            # An earlier attempt may have reached Instagram but lost the reply.
            # Container status can lag, so wait before trusting "FINISHED".
            if self.clock() - _aware(started) < timedelta(seconds=PUBLISH_SETTLE_SECONDS):
                raise GraphError("Verifying previous publish attempt", transient=True)
        # Last chance to stop: /pause, quiet hours or a new rule must win right up
        # to the point of no return. The container is kept, so resuming reuses it.
        blocked = self._gate_decision(job, final=True)
        if blocked is not None:
            raise Blocked(blocked)
        self._set(jid, publish_started_at=self.clock())
        media_id = self.graph.publish_container(ig_id, token, container)
        self._set(jid, media_id=media_id)
        self._log(jid, "media_publish", True, {"media_id": media_id})
        self._finish(job, media_id, token)

    def _precheck_quota(self, ig_id, token, jid):
        try:
            lim = self.graph.publishing_limit(ig_id, token)
        except GraphError as exc:
            if exc.auth:
                raise
            return  # quota endpoint is advisory; real limit is enforced on publish
        if lim["total"] and lim["usage"] >= lim["total"]:
            raise GraphError("Daily Instagram publishing limit reached",
                             code=4, transient=True)

    def _create_container(self, job, assets, caption, ig_id, token):
        fmt, jid = job["format"], job["_id"]
        variant = VARIANT_FOR[fmt]

        def url_for(asset):
            data = self.buffer.get_media(asset["_id"], variant)
            preflight_image(data, fmt)
            return self.media_url(asset["creative"]["variants"][variant])

        if fmt == "reel":
            asset = assets[0]
            preflight_reel(self.buffer.get_media(asset["_id"], variant), asset)
            cid = self.graph.create_container(
                ig_id, token, media_type="REELS", caption=caption, share_to_feed=True,
                video_url=self.media_url(asset["creative"]["variants"][variant], ext="mp4"))
        elif fmt == "photo":
            cid = self.graph.create_container(ig_id, token,
                                              image_url=url_for(assets[0]),
                                              caption=caption)
        elif fmt == "story":
            cid = self.graph.create_container(ig_id, token, media_type="STORIES",
                                              image_url=url_for(assets[0]))
        else:  # carousel
            urls = [url_for(a) for a in assets]
            children = list(job.get("child_ids") or [])
            for url in urls[len(children):]:
                children.append(self.graph.create_container(
                    ig_id, token, image_url=url, carousel_item=True))
                self._set(jid, child_ids=children)   # resumable
            cid = self.graph.create_container(ig_id, token, media_type="CAROUSEL",
                                              children=children, caption=caption)
        self._log(jid, "create_container", True, {"container": cid, "format": fmt})
        return cid

    def _wait_finished(self, container, token, jid):
        last = None
        for i in range(self.cfg.poll_attempts):
            code, detail = self.graph.container_status(container, token)
            last = code
            if code in ("FINISHED", "PUBLISHED", "ERROR", "EXPIRED"):
                self._log(jid, "container_status", code != "ERROR",
                          {"status_code": code, "detail": detail})
                return code
            self.sleep(self.cfg.poll_interval)
        raise GraphError(f"Container still processing (last status: {last})",
                         transient=True)

    def _finish(self, job, media_id, token):
        jid = job["_id"]
        permalink = None
        try:   # best-effort verification of the final live status
            info = self.graph.media_info(media_id, token)
            permalink = info.get("permalink")
            self._log(jid, "verify", True, info)
        except GraphError as exc:
            self._log(jid, "verify", False, exc.summary())
        self._set(jid, status=PUBLISHED, media_id=media_id, permalink=permalink,
                  last_error=None, published_at=self.clock())
        for aid in job["asset_ids"]:
            self.buffer.assets.update_one(
                _owned_query(Principal.from_telegram(job["telegram_id"]), {"_id": aid}),
                {"$set": {"last_published_at": self.clock()}})

    # ---- queries ---------------------------------------------------------
    def recent_jobs(self, telegram_id, limit=5):
        rows = self.jobs.find(_owned_query(Principal.from_telegram(telegram_id), {"telegram_id": str(telegram_id)}))
        return sorted(rows, key=lambda j: j["created_at"], reverse=True)[:limit]

    def due_jobs(self):
        """Jobs a worker may run now: due queued/retry jobs, plus `running` jobs whose
        lease expired (the process that owned them died / restarted)."""
        now = self.clock()
        rows = []
        for j in self.jobs.find({"status": {"$in": [QUEUED, RETRY, RUNNING]}}):
            # This polling worker is Telegram-only. Never surface web-owned,
            # unknown-owner, or identity-mismatched jobs to it.
            if not self._telegram_worker_eligible(j):
                continue
            if j["status"] == RUNNING:
                lease = j.get("lease_until")
                if lease is None or _aware(lease) >= now:
                    continue                      # still owned by a live worker
            elif _aware(j["next_attempt_at"]) > now:
                continue
            rows.append(j)
        return sorted(rows, key=lambda j: _aware(j.get("next_attempt_at") or j["created_at"]))
