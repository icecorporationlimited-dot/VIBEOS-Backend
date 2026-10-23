"""Step 11: bridge a selected, ready story-library item into VibeOS publishing.

queue_story(...)                  user taps "publish this story" -> ContentBuffer -> PublishService job.
cancel_expired_story_jobs(...)    scheduler sweep: cancel jobs that have not started once their trend is
                                  expired / unverified / deleted (Mongo TTL removes old trends, so the job's
                                  own `story_expires_at` is the source of truth, not the trends collection).

Running jobs are never force-cancelled: publishing may already have reached Instagram, and pretending
otherwise risks duplicate/uncertain posts.
"""
import logging
from datetime import datetime, timezone

from publishing.identity import Principal
from publishing.service import PublishError
from . import feed, mongo_store, safety, verify
from .db import FEED, TRENDS
from .variants import apply_crop

log = logging.getLogger(__name__)

_PENDING = ["queued", "retry", "held", "needs_reauth"]     # same set PublishService.cancel() accepts


class StoryPublishError(ValueError):
    """Story cannot safely be sent to the publishing queue (message is safe to show the user)."""


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _enqueue(publish_service, principal, asset_id, scheduled_at):
    source = "scheduled" if scheduled_at else "manual"
    if principal.kind == "telegram":                       # legacy entry point, same as trends/auto_story.py
        return publish_service.enqueue(principal.subject, [asset_id], "story",
                                       scheduled_at=scheduled_at, source=source)
    return publish_service.enqueue_for(principal, [asset_id], "story",
                                       scheduled_at=scheduled_at, source=source)


def queue_story(db, content_buffer, publish_service, principal: Principal,
                story_asset_id: str, scheduled_at=None) -> dict:
    """Queue one feed-owned story for Instagram Story publishing.

    `principal` must come from an already-authenticated adapter (web session / Telegram).
    The feed's user_id is principal.subject. Raises StoryPublishError with a user-safe message.
    """
    if not isinstance(principal, Principal):
        raise StoryPublishError("A verified Principal is required.")

    story_asset_id = str(story_asset_id)
    user_id = principal.subject
    entry = feed.get_entry(db, user_id, story_asset_id)
    if not entry:
        raise StoryPublishError("Story not found in your feed.")

    now = datetime.now(timezone.utc)
    trend = db[TRENDS].find_one({"trend_id": entry["trend_id"]})
    expires = _aware((trend or {}).get("expires_at"))
    if not trend or trend.get("verification_status") != "verified" or not expires or expires <= now:
        raise StoryPublishError("This trend has expired or is no longer verified.")
    # Last gate right before queueing: the stored status can be hours old, so re-run the live safety gate
    # (sensitive topics in any publisher headline, trust, disputes, number / death corroboration).
    live = verify.gate(trend, now)
    if live["status"] != "verified":
        log.warning("story %s blocked by live safety gate: %s", story_asset_id, live["reasons"])
        raise StoryPublishError("This trend no longer passes the safety check.")

    when = _aware(scheduled_at)
    if when is not None:
        if when >= expires:
            raise StoryPublishError("Schedule it before the trend expires.")
        if when < now:
            when = None                                     # past time = post as soon as possible

    asset = feed.asset_by_id(db, story_asset_id)
    if (not asset or asset.get("quality_status") != "ready" or not asset.get("rendered_image_url")
            or asset.get("claimed_by") != user_id):
        raise StoryPublishError("Story image is not ready to publish.")

    try:
        # Publish exactly what this user was shown: same crop as /api/story-feed/image.
        image_bytes = apply_crop(mongo_store.load_image(db, asset["rendered_image_url"]), entry.get("crop"))
    except Exception:
        log.exception("could not load story image %s", story_asset_id)
        raise StoryPublishError("Story image has expired. Pick another story.") from None

    res = content_buffer.add_trend_story(
        principal, image_bytes,
        trend_key=f'{trend["trend_id"]}:{story_asset_id}',
        title=trend.get("title", "Trend story"),
        meta={"decision": {"source": "auto_trend_story", "trend_id": trend["trend_id"],
                           "mood": asset.get("mood"), "angle": asset.get("angle"),
                           # what the card really says: PolicyEngine checks the user's blocked topics against it
                           "trend_title": trend.get("title", ""), "copy": list(asset.get("copy") or []),
                           "source_titles": [t for _, t in safety.source_headlines(trend)]}},
    )
    buffered = res["asset"]                                 # add_trend_story returns a dict, not a tuple

    try:
        job = _enqueue(publish_service, principal, buffered["_id"], when)
    except PublishError as exc:                             # NotConnected is a PublishError subclass
        if not res["duplicate"]:
            try:
                content_buffer.reject(buffered["_id"], "story_enqueue_failed")
            except Exception:
                log.exception("could not reject buffered story %s", buffered["_id"])
        raise StoryPublishError(str(exc)) from None

    # Metadata lives beside the publisher's idempotency key. `trend_expires_at` is also read by the
    # existing Worker._expire_trend_jobs, so expiry is enforced even if the story sweep is not running.
    publish_service.jobs.update_one(
        {"_id": job["_id"]},
        {"$set": {"story_trend_id": trend["trend_id"], "story_asset_id": story_asset_id,
                  "story_expires_at": expires, "trend_expires_at": expires,
                  "story_variant_id": entry.get("variant_id"), "updated_at": now}},
    )
    # "posted" here means "sent to publishing"; the job status stays canonical.
    db[FEED].update_one(
        {"user_id": user_id, "trend_id": trend["trend_id"]},
        {"$set": {"status": "posted", "publish_job_id": job["_id"], "updated_at": now}},
    )
    return {"job_id": job["_id"], "status": job.get("status", "queued"),
            "duplicate": bool(job.get("duplicate")), "trend_id": trend["trend_id"],
            "story_asset_id": story_asset_id, "expires_at": expires.isoformat()}


def cancel_expired_story_jobs(db, jobs_collection, now=None) -> int:
    """Cancel not-yet-started story jobs whose trend expired, was un-verified, or was deleted.

    Safe to run repeatedly. Cancelled jobs flip their feed entry to "skipped".
    """
    now = now or datetime.now(timezone.utc)
    pending = list(jobs_collection.find({"story_trend_id": {"$exists": True}, "status": {"$in": _PENDING}}))
    if not pending:
        return 0
    live = {t["trend_id"]: t for t in db[TRENDS].find({"trend_id": {"$in": list({j["story_trend_id"] for j in pending})}})}
    cancelled = 0
    for job in pending:
        trend = live.get(job["story_trend_id"])
        expires = _aware(job.get("story_expires_at"))
        dead = (trend is None or trend.get("verification_status") != "verified"
                or (expires is not None and expires <= now))
        if not dead:
            continue
        res = jobs_collection.update_one(                    # status re-checked atomically: never touches a running job
            {"_id": job["_id"], "status": {"$in": _PENDING}},
            {"$set": {"status": "cancelled", "hold_reason": "source trend expired",
                      "cancelled_reason": "story_trend_expired", "updated_at": now}})
        if getattr(res, "modified_count", getattr(res, "matched_count", 0)):
            cancelled += 1
            db[FEED].update_one({"publish_job_id": job["_id"]}, {"$set": {"status": "skipped", "updated_at": now}})
    return cancelled
