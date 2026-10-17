"""
content_buffer.py — Phase 1: Smart Content Buffer.

Durable media storage + per-asset status tracking:

    uploaded -> analyzed -> selected -> ready
                   \\-> rejected (unreadable file, duplicate, ...)

Everything here takes its collection/blob store by injection, so it is fully
testable without MongoDB (see tests/test_phase1.py).
"""
import hashlib
import io
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import media_quality as mq
import shortlist_engine
from publishing.identity import Principal, owner_filter

logger = logging.getLogger(__name__)

UPLOADED, ANALYZED, SELECTED, READY, REJECTED = (
    "uploaded", "analyzed", "selected", "ready", "rejected",
)
STATUSES = (UPLOADED, ANALYZED, SELECTED, READY, REJECTED)

# status -> statuses it may move to
TRANSITIONS = {
    UPLOADED: {ANALYZED, REJECTED},
    ANALYZED: {SELECTED, REJECTED},
    SELECTED: {READY, ANALYZED, REJECTED},
    READY: {REJECTED},
    REJECTED: set(),
}

MAX_MEDIA_BYTES = {"photo": 10 * 1024 * 1024, "video": 50 * 1024 * 1024}


class InvalidTransition(Exception):
    pass


class MediaTooLarge(Exception):
    pass


def _daily_upload_limit():
    try:
        return max(1, int(os.environ.get("USER_DAILY_UPLOADS", "300")))
    except ValueError:
        return 300


class QuotaExceeded(MediaTooLarge):
    """Per-user rolling 24h upload cap reached (protects shared storage/AI cost).

    Subclasses MediaTooLarge so any existing caller that already skips oversized
    media keeps working unchanged.
    """


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Blob stores
# ---------------------------------------------------------------------------
class GridFSBlobStore:
    """Durable storage in MongoDB GridFS (survives restarts/redeploys)."""

    def __init__(self, db, collection="media"):
        import gridfs
        self._fs = gridfs.GridFS(db, collection=collection)

    def put(self, data: bytes, filename: str = "") -> str:
        return str(self._fs.put(data, filename=filename))

    def get(self, blob_id: str) -> bytes:
        from bson import ObjectId
        return self._fs.get(ObjectId(blob_id)).read()

    def delete(self, blob_id: str) -> None:
        from bson import ObjectId
        self._fs.delete(ObjectId(blob_id))


class MemoryBlobStore:
    """In-memory store for tests."""

    def __init__(self):
        self._data = {}

    def put(self, data: bytes, filename: str = "") -> str:
        blob_id = uuid.uuid4().hex
        self._data[blob_id] = bytes(data)
        return blob_id

    def get(self, blob_id: str) -> bytes:
        return self._data[blob_id]

    def delete(self, blob_id: str) -> None:
        self._data.pop(blob_id, None)


# ---------------------------------------------------------------------------
# Buffer
# ---------------------------------------------------------------------------
def _scope(user):
    """Return (query, stamp) that scope assets to exactly one owner.

    * plain Telegram ID (legacy entry point): unchanged behaviour,
      query {"telegram_id": id}, stamp {"telegram_id": id}.
    * web Principal: query {"owner_id": "web:<id>"}, stamp {"owner_id": ...}.
      Web assets never carry a telegram_id and are never matched by one.
    * Telegram Principal: treated like its plain Telegram ID.
    """
    if isinstance(user, Principal):
        if user.kind == "web":
            return {"owner_id": user.owner_id}, {"owner_id": user.owner_id}
        user = user.subject
    owner = str(user)
    return {"telegram_id": owner}, {"telegram_id": owner}


def owner_query(user):
    """Public form of the asset ownership selector: one Telegram ID or Principal -> filter."""
    return _scope(user)[0]


class ContentBuffer:
    def __init__(self, assets, blobs):
        self.assets = assets
        self.blobs = blobs

    def ensure_indexes(self):
        self.assets.create_index([("owner_id", 1), ("sha256", 1)], sparse=True)
        self.assets.create_index([("owner_id", 1), ("batch_id", 1)], sparse=True)
        self.assets.create_index([("owner_id", 1), ("status", 1)], sparse=True)
        self.assets.create_index([("owner_id", 1), ("created_at", 1)], sparse=True)
        self.assets.create_index([("telegram_id", 1), ("sha256", 1)])
        self.assets.create_index([("telegram_id", 1), ("batch_id", 1)])
        self.assets.create_index([("telegram_id", 1), ("status", 1)])
        self.assets.create_index([("telegram_id", 1), ("created_at", 1)])

    # ---- quota -----------------------------------------------------------
    def _check_quota(self, user):
        """Raise QuotaExceeded if this user already added too many assets in 24h.

        Counts only this user's rows, so one user can never use up another's
        allowance. Skipped for stores without count_documents (test doubles).
        """
        count = getattr(self.assets, "count_documents", None)
        if count is None:
            return
        since = _now() - timedelta(hours=24)
        flt, _ = _scope(user)
        used = count({**flt, "created_at": {"$gte": since}})
        if used >= _daily_upload_limit():
            raise QuotaExceeded("daily upload limit reached")

    # ---- intake ----------------------------------------------------------
    def add_asset(self, user_id, data: bytes, kind="photo", batch_id=None,
                  filename="", source=None) -> dict:
        """Store media durably and register it as `uploaded`.

        Returns {"asset": doc, "duplicate": bool}. An exact byte-for-byte
        re-upload returns the existing asset instead of creating a new one.
        """
        if kind not in MAX_MEDIA_BYTES:
            raise ValueError(f"Unsupported media kind: {kind}")
        if len(data) > MAX_MEDIA_BYTES[kind]:
            raise MediaTooLarge(f"{kind} exceeds size limit")

        flt, stamp = _scope(user_id)
        digest = hashlib.sha256(data).hexdigest()

        existing = self.assets.find_one({
            **flt,
            "sha256": digest,
            "status": {"$in": [UPLOADED, ANALYZED, SELECTED, READY]},
        })
        if existing:
            return {"asset": existing, "duplicate": True}

        self._check_quota(user_id)
        blob_id = self.blobs.put(data, filename=filename)
        now = _now()
        doc = {
            "_id": uuid.uuid4().hex,
            **stamp,
            "kind": kind,
            "batch_id": batch_id,
            "filename": filename,
            "source": source,
            "sha256": digest,
            "size_bytes": len(data),
            "blob_id": blob_id,
            "status": UPLOADED,
            "analysis": None,
            "creative": None,
            "reject_reason": None,
            "history": [{"status": UPLOADED, "at": now}],
            "created_at": now,
            "updated_at": now,
        }
        self.assets.insert_one(doc)
        return {"asset": doc, "duplicate": False}

    def add_reel(self, user_id, data: bytes, *, meta: dict, caption: str, hashtags,
                 vibe="natural", source_ids=(), batch_id=None) -> dict:
        """Register a rendered Reel MP4 directly as `ready` (kind="video").

        The Reel is built from photos that were already approved/edited, so it
        skips analysis. `meta` is the ffprobe result and is checked again at
        publish time. Returns {"asset": doc, "duplicate": bool}.
        """
        if len(data) > MAX_MEDIA_BYTES["video"]:
            raise MediaTooLarge("video exceeds size limit")
        flt, stamp = _scope(user_id)
        digest = hashlib.sha256(data).hexdigest()
        existing = self.assets.find_one({
            **flt, "sha256": digest,
            "status": {"$in": [UPLOADED, ANALYZED, SELECTED, READY]}})
        if existing:
            return {"asset": existing, "duplicate": True}
        self._check_quota(user_id)
        blob_id = self.blobs.put(data, filename=f"reel_{digest[:12]}.mp4")
        now = _now()
        doc = {
            "_id": uuid.uuid4().hex, **stamp, "kind": "video",
            "batch_id": batch_id, "filename": f"reel_{digest[:12]}.mp4",
            "source": "reel_builder", "sha256": digest, "size_bytes": len(data),
            "blob_id": blob_id, "status": READY,
            "analysis": {"video": dict(meta), "quality_score": None,
                         "source_asset_ids": list(source_ids)},
            "creative": {"caption": caption, "hashtags": list(hashtags), "vibe": vibe,
                         "variants": {"reel": blob_id}},
            "reject_reason": None,
            "history": [{"status": READY, "at": now}],
            "created_at": now, "updated_at": now,
        }
        self.assets.insert_one(doc)
        return {"asset": doc, "duplicate": False}

    def add_trend_story(self, user_id, data: bytes, *, trend_key: str, title: str,
                        meta: dict) -> dict:
        """Register a rendered trend Story card as a ready, Story-only asset.

        One asset per (user, trend): a trend that was already used returns
        {"duplicate": True}. The route is pinned to "story" so the normal photo
        autofill never turns the card into a feed post.
        """
        flt, stamp = _scope(user_id)
        existing = self.assets.find_one({**flt, "trend_key": trend_key})
        if existing:
            return {"asset": existing, "duplicate": True}
        if len(data) > MAX_MEDIA_BYTES["photo"]:
            raise MediaTooLarge("photo exceeds size limit")
        asset_id = uuid.uuid4().hex
        blob_id = self.blobs.put(data, filename=f"trend_{asset_id[:8]}.jpg")
        now = _now()
        doc = {
            "_id": asset_id, **stamp, "kind": "photo", "batch_id": None,
            "filename": f"trend_{asset_id[:8]}.jpg", "source": "trend_story",
            "trend_key": trend_key, "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data), "blob_id": blob_id, "status": READY,
            "analysis": {"quality_score": None, "trend_title": title[:200]},
            "creative": {"caption": "", "hashtags": [], "vibe": "natural",
                         "variants": {"story": blob_id}},
            "route": {"format": "story", "asset_ids": [asset_id], "group_id": None,
                      "decision": meta.get("decision") or {}},
            "reject_reason": None, "history": [{"status": READY, "at": now}],
            "created_at": now, "updated_at": now,
        }
        self.assets.insert_one(doc)
        return {"asset": doc, "duplicate": False}

    # ---- status machine --------------------------------------------------
    def _transition(self, asset_id, to_status, set_fields=None):
        current = self.assets.find_one({"_id": asset_id})
        if not current:
            raise KeyError(f"Unknown asset {asset_id}")

        allowed_from = [s for s, nxt in TRANSITIONS.items() if to_status in nxt]
        now = _now()
        fields = dict(set_fields or {})
        fields.update({"status": to_status, "updated_at": now})

        # Atomic: only applies if the asset is still in an allowed source state.
        result = self.assets.update_one(
            {"_id": asset_id, "status": {"$in": allowed_from}},
            {"$set": fields, "$push": {"history": {"status": to_status, "at": now}}},
        )
        if result.matched_count != 1:
            raise InvalidTransition(
                f"{current['status']} -> {to_status} not allowed for {asset_id}"
            )

    def reject(self, asset_id, reason):
        self._transition(asset_id, REJECTED, {"reject_reason": reason})

    # ---- analysis --------------------------------------------------------
    def analyze(self, asset_id) -> dict:
        """uploaded -> analyzed; reject only unreadable/corrupt images at intake.

    Fixable quality issues remain as ranking hints so the best available photos
    can still be shortlisted and passed through adaptive editing.
    """
        asset = self.assets.find_one({"_id": asset_id})
        if not asset:
            raise KeyError(f"Unknown asset {asset_id}")
        if asset["status"] != UPLOADED:
            return asset

        if asset["kind"] != "photo":
            # Video analysis is not implemented yet: store it, keep it out of
            # automatic selection rather than guessing.
            analysis = {"issues": [], "notes": ["video_not_analyzed"],
                        "quality_score": None, "dhash": None}
            self._transition(asset_id, ANALYZED, {"analysis": analysis})
            return self.assets.find_one({"_id": asset_id})

        analysis = mq.analyze_image(self.blobs.get(asset["blob_id"]))
        self._transition(asset_id, ANALYZED, {"analysis": analysis})

        if mq.has_hard_issue(analysis):
            hard = sorted(set(analysis["issues"]) & mq.HARD_ISSUES)
            self.reject(asset_id, "quality:" + ",".join(hard))

        return self.assets.find_one({"_id": asset_id})

    def analyze_batch(self, user_id, batch_id) -> list:
        out = []
        for asset in self.list_assets(user_id, batch_id=batch_id, status=UPLOADED):
            try:
                out.append(self.analyze(asset["_id"]))
            except Exception:
                logger.exception("Analysis failed for %s", asset["_id"])
        return out

    # ---- selection -------------------------------------------------------
    def select_shortlist(self, user_id, batch_id, n=3) -> dict:
        """Pick up to n best, mutually-different photos from a batch.

        Smart shortlist (see shortlist_engine.py):
          * only analyzed, readable photos (quality problems affect ranking, not eligibility)
          * exact copies of a better frame, or of anything already selected/ready in
            another batch, are rejected (never post the same picture twice)
          * bursts / same-moment shots are grouped and only the best frame survives
            (sharper, eyes open, smiling)
          * the survivors are ordered strongest-AND-most-varied first (MMR), and that
            order is stored on each asset as `shortlist_rank`
        The return shape is unchanged: selected / skipped / rejected.
        """
        owner = user_id if isinstance(user_id, Principal) else str(user_id)
        candidates = [
            {"id": a["_id"], "analysis": a["analysis"]}
            for a in self.list_assets(owner, batch_id=batch_id, status=ANALYZED)
            if a["kind"] == "photo" and a["analysis"]
            and (a["analysis"].get("dhash") or a["analysis"].get("phash"))
        ]

        used = [
            a["analysis"]
            for st in (SELECTED, READY)
            for a in self.list_assets(owner, status=st)
            if a.get("batch_id") != batch_id
            and a.get("analysis")
            and (a["analysis"].get("dhash") or a["analysis"].get("phash"))
        ]

        plan = shortlist_engine.plan_shortlist(candidates, used, n)

        for asset_id, reason in plan["rejected"]:
            self.reject(asset_id, reason)
        for asset_id in plan["selected"]:
            meta = plan["info"].get(asset_id, {})
            self._transition(asset_id, SELECTED, {
                "shortlist_rank": meta.get("rank"),
                "shortlist_cluster_size": meta.get("cluster_size", 1),
                "shortlist_burst_winner": bool(meta.get("burst_winner")),
            })

        return {
            "selected": list(plan["selected"]),
            "skipped": list(plan["skipped"]),
            "rejected": [
                a["_id"] for a in self.list_assets(owner, batch_id=batch_id, status=REJECTED)
            ],
        }

    # ---- creative variants ----------------------------------------------
    def prepare_ready(self, asset_id, caption, hashtags, vibe="natural",
                      sizes=("post", "story"), edit_fn=None) -> dict:
        """selected -> ready: render approved variants and attach copy."""
        asset = self.assets.find_one({"_id": asset_id})
        if not asset:
            raise KeyError(f"Unknown asset {asset_id}")
        if asset["status"] != SELECTED:
            raise InvalidTransition(f"{asset['status']} -> ready not allowed")

        if edit_fn is None:
            # Adaptive photo finishing: existing VibeOS look + per-image corrections.
            from creative_studio.photo_engine import edit_photo_adaptive as edit_fn

        original = self.blobs.get(asset["blob_id"])
        variants = {}
        try:
            for size in sizes:
                out = edit_fn(original, vibe, size=size)
                data = out.getvalue() if isinstance(out, io.BytesIO) else out
                variants[size] = self.blobs.put(data, filename=f"{asset_id}_{size}.jpg")
        except Exception:
            for blob_id in variants.values():
                self.blobs.delete(blob_id)
            raise

        creative = {
            "caption": caption,
            "hashtags": list(hashtags),
            "vibe": vibe,
            "variants": variants,
        }
        self._transition(asset_id, READY, {"creative": creative})
        return self.assets.find_one({"_id": asset_id})

    # ---- approval helpers -----------------------------------------------
    def asset_for_file(self, user_id, batch_id, filename, status=None):
        """Find the asset registered for a Telegram file_id inside a batch."""
        for a in self.list_assets(user_id, batch_id=batch_id, status=status):
            if a.get("filename") == filename:
                return a
        return None

    def release(self, asset_id):
        """selected -> analyzed: shortlisted but not chosen; frees the photo
        so it does not block similar photos in later batches."""
        try:
            self._transition(asset_id, ANALYZED)
        except InvalidTransition:
            pass

    def discard(self, asset_id, reason="discarded_by_user"):
        """User said no: selected/ready -> rejected. Safe to call twice."""
        try:
            self.reject(asset_id, reason)
        except (InvalidTransition, KeyError):
            pass

    # ---- queries ---------------------------------------------------------
    def list_assets(self, user_id, status=None, batch_id=None) -> list:
        flt, _ = _scope(user_id)
        if status:
            flt["status"] = status
        if batch_id is not None:
            flt["batch_id"] = batch_id
        return sorted(self.assets.find(flt), key=lambda a: a["created_at"])

    def list_photo_ready(self, user_id) -> list:
        """Ready *photos* that can be posted on their own: no Reels, nothing
        already consumed by a Reel, and no auto-made trend Stories."""
        return [a for a in self.list_assets(user_id, status=READY)
                if a.get("kind", "photo") == "photo"
                and a.get("source") != "trend_story"
                and (a.get("route") or {}).get("format") != "reel_source"]

    def get_media(self, asset_id, variant=None) -> bytes:
        """Legacy internal lookup by asset ID.

        Keep this for existing trusted Telegram pipeline callers. Browser-facing
        routes must use get_media_for() so an asset ID alone never grants access.
        """
        asset = self.assets.find_one({"_id": asset_id})
        if not asset:
            raise KeyError(f"Unknown asset {asset_id}")
        if variant:
            return self.blobs.get(asset["creative"]["variants"][variant])
        return self.blobs.get(asset["blob_id"])

    def get_media_for(self, principal, asset_id, variant=None) -> bytes:
        """Read media only when the verified principal owns the asset.

        Web principals match owner_id only. Telegram principals may read legacy
        rows that have no owner_id and still use telegram_id during migration.
        Use this method for any browser/session-authenticated media endpoint.
        """
        if not isinstance(principal, Principal):
            raise TypeError("A verified Principal is required.")
        asset = self.assets.find_one({
            "$and": [
                {"_id": asset_id},
                owner_filter(principal),
            ]
        })
        if not asset:
            # Do not reveal whether another tenant's asset ID exists.
            raise KeyError("Media not found")
        if variant:
            return self.blobs.get(asset["creative"]["variants"][variant])
        return self.blobs.get(asset["blob_id"])

    def delete_asset(self, user_id, asset_id) -> bool:
        """Permanently delete one asset owned by this Telegram user and its blobs.

        The owner filter is included in both lookup and deletion so guessing
        another user's asset ID cannot remove their content.
        """
        flt, _ = _scope(user_id)
        asset = self.assets.find_one({"_id": str(asset_id), **flt})
        if not asset:
            return False

        blob_ids = set()
        if asset.get("blob_id"):
            blob_ids.add(str(asset["blob_id"]))
        variants = (asset.get("creative") or {}).get("variants") or {}
        if isinstance(variants, dict):
            blob_ids.update(str(value) for value in variants.values() if value)

        # Delete the tenant-owned row first; only then clean up its stored files.
        result = self.assets.delete_one({"_id": asset["_id"], **flt})
        if result.deleted_count != 1:
            return False
        for blob_id in blob_ids:
            try:
                self.blobs.delete(blob_id)
            except Exception:
                # Keep deletion moving if a stale/missing blob is encountered.
                logger.warning("Could not remove blob for deleted asset %s", asset_id,
                               exc_info=True)
        return True

    def delete_all_assets(self, user_id) -> int:
        """Permanently delete every content-buffer asset belonging to this user."""
        owner = user_id if isinstance(user_id, Principal) else str(user_id)
        ids = [a["_id"] for a in self.list_assets(owner)]
        deleted = 0
        for asset_id in ids:
            if self.delete_asset(owner, asset_id):
                deleted += 1
        return deleted

    def stats(self, user_id) -> dict:
        counts = {s: 0 for s in STATUSES}
        for asset in self.list_assets(user_id):
            counts[asset["status"]] += 1
        return counts

    # ---- one-call pipeline ----------------------------------------------
    def process_batch(self, user_id, batch_id, photos, n=3) -> dict:
        """photos: list of (bytes, filename). Intake -> analyze -> shortlist."""
        added, duplicates, too_large, quota_blocked = [], 0, 0, 0
        for data, filename in photos:
            try:
                res = self.add_asset(user_id, data, "photo", batch_id, filename)
            except QuotaExceeded:
                quota_blocked += 1
                continue
            except MediaTooLarge:
                too_large += 1
                continue
            if res["duplicate"]:
                duplicates += 1
            else:
                added.append(res["asset"]["_id"])

        self.analyze_batch(user_id, batch_id)
        report = self.select_shortlist(user_id, batch_id, n=n)
        report.update({"added": added, "exact_duplicates": duplicates,
                       "too_large": too_large,
                       "quota_blocked": quota_blocked})
        return report
