"""Step 11: bridge a selected, ready story-library item into VibeOS publishing.

Call ``queue_story`` from the authenticated app action that means “publish this
story”. It imports the rendered image into the caller's existing ContentBuffer,
then uses PublishService's normal owner/account/policy/idempotency checks.

Call ``cancel_expired_story_jobs`` periodically from the publishing worker (or
scheduler). It cancels jobs that have not started when their trend expires.
Running jobs are deliberately not force-cancelled: publishing may already have
reached Instagram, and pretending otherwise risks duplicate/uncertain posts.
"""
from datetime import datetime, timezone

from publishing.identity import Principal
from . import feed, mongo_store
from .db import FEED, TRENDS

_PENDING = ["queued", "retry", "held", "needs_reauth"]


class StoryPublishError(ValueError):
    """Story cannot safely be sent to the publishing queue."""


def queue_story(db, content_buffer, publish_service, principal: Principal,
                story_asset_id: str, scheduled_at=None) -> dict:
    """Queue one feed-owned story for Instagram Story publishing.

    ``principal`` must come from an already-authenticated identity adapter.
    Returns a small job summary; the raw Mongo job is not exposed to clients.
    """
    if not isinstance(principal, Principal):
        raise StoryPublishError("A verified Principal is required.")

    user_id = principal.subject
    entry = feed.get_entry(db, user_id, str(story_asset_id))
    if not entry:
        raise StoryPublishError("Story not found in this user's feed.")

    trend = db[TRENDS].find_one({"trend_id": entry["trend_id"]})
    now = datetime.now(timezone.utc)
    if (not trend or trend.get("verification_status") != "verified"
            or not trend.get("expires_at") or trend["expires_at"] <= now):
        raise StoryPublishError("This trend has expired or is no longer verified.")

    asset = feed.asset_by_id(db, str(story_asset_id))
    if not asset or asset.get("quality_status") != "ready" or not asset.get("rendered_image_url"):
        raise StoryPublishError("Story image is not ready to publish.")

    image_bytes = mongo_store.load_image(db, asset["rendered_image_url"])
    buffered, _duplicate = content_buffer.add_trend_story(
        principal, image_bytes,
        trend_key=f'{trend["trend_id"]}:{story_asset_id}',
        title=trend.get("title", "Trend story"),
        meta={"decision": {"source": "auto_trend_story", "trend_id": trend["trend_id"],
                            "mood": asset.get("mood"), "angle": asset.get("angle")}},
    )

    job = publish_service.enqueue_for(
        principal, [buffered["_id"]], "story",
        scheduled_at=scheduled_at,
        source="scheduled" if scheduled_at else "manual",
    )
    # Metadata is separate from the publisher's idempotency key and lets the
    # expiry sweep find/cancel this job without changing the publishing service.
    publish_service.jobs.update_one(
        {"_id": job["_id"]},
        {"$set": {"story_trend_id": trend["trend_id"],
                  "story_asset_id": str(story_asset_id),
                  "story_expires_at": trend["expires_at"],
                  "story_variant_id": entry.get("variant_id"),
                  "updated_at": now}},
    )
    # Only mark as posted after the queue accepts the job. The feed state means
    # “sent to publishing”, not “successfully published”; job status is canonical.
    db[FEED].update_one(
        {"user_id": user_id, "trend_id": trend["trend_id"]},
        {"$set": {"status": "posted", "publish_job_id": job["_id"], "updated_at": now}},
    )
    return {"job_id": job["_id"], "status": job.get("status", "queued"),
            "duplicate": bool(job.get("duplicate")), "trend_id": trend["trend_id"],
            "story_asset_id": str(story_asset_id), "expires_at": trend["expires_at"]}


def cancel_expired_story_jobs(db, jobs_collection, now=None) -> int:
    """Cancel queued story jobs whose source trend expired; safe to run repeatedly."""
    now = now or datetime.now(timezone.utc)
    expired_ids = [t["trend_id"] for t in db[TRENDS].find(
        {"expires_at": {"$lte": now}}, {"trend_id": 1})]
    if not expired_ids:
        return 0
    result = jobs_collection.update_many(
        {"story_trend_id": {"$in": expired_ids},
         "status": {"$in": _PENDING}},
        {"$set": {"status": "cancelled", "hold_reason": "source trend expired",
                  "updated_at": now, "cancelled_reason": "story_trend_expired"}},
    )
    return int(getattr(result, "modified_count", 0))
