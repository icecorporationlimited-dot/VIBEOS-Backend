"""story_engine/auto_publish.py — story library -> Instagram Story, with ZERO human taps.

Called by publishing.scheduler.Worker every few minutes. For each owner who has switched
autopilot + trend stories on, it:
  1. skips if paused / Story format not allowed / Instagram not connected
  2. skips if a story job is already waiting, or the per-day cap is reached
  3. claims the best fresh, quality-"ready" story for that user (feed.pick_for_user — atomic, so two users
     never get the same asset and one user never gets the same trend twice)
  4. hands it to publishing_integration.queue_story, i.e. the SAME path a manual tap used.

Everything after step 4 is unchanged: PolicyEngine (quiet hours, daily limit, min gap, blocked topics,
/pause) decides when it really goes out, and Worker._expire_trend_jobs cancels it if the trend
expires first. Nothing here bypasses those gates.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

from publishing.identity import owner_filter
from . import feed
from .db import FEED, TRENDS
from . import safety
from .publishing_integration import StoryPublishError, queue_story

log = logging.getLogger(__name__)

MAX_PER_DAY = int(os.environ.get("STORY_AUTO_MAX_PER_DAY", "1"))     # per user, rolling 24h
MAX_PICKS = 5                                                         # blocked-topic skips tried per run
_OPEN_STATUSES = ["queued", "retry", "held", "needs_reauth", "needs_attention", "running"]


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def posted_last_24h(db, user_id, now) -> int:
    rows = db[FEED].find({"user_id": user_id, "status": "posted"})
    return sum(1 for r in rows if (_aware(r.get("updated_at") or r.get("created_at")) or now) > now - timedelta(hours=24))


def has_open_story_job(jobs, principal) -> bool:
    return jobs.find_one({**owner_filter(principal), "story_trend_id": {"$exists": True},
                          "status": {"$in": _OPEN_STATUSES}}) is not None


def blocked_by_user(db, entry, blocked_topics):
    """The user's own blocked topic found in the headline, any publisher headline or the story text, else None."""
    if not blocked_topics:
        return None
    trend = db[TRENDS].find_one({"trend_id": entry.get("trend_id")}) or {}
    titles = [t for _, t in safety.source_headlines(trend)]
    return safety.topic_hit(blocked_topics, entry.get("trend_title"), trend.get("title"),
                            " ".join(entry.get("copy") or []), *titles)


def publish_one(db, buffer, service, principal, now=None, max_per_day=None, blocked_topics=None):
    """Pick + queue at most one story for this owner. Returns a dict, or None if nothing was queued.

    blocked_topics: the owner's autopilot `blocked_topics`. A story that mentions one is skipped here (and
    never queued), so a policy HOLD can not sit in the queue and block every later story.
    """
    now = now or _now()
    cap = MAX_PER_DAY if max_per_day is None else max_per_day
    user_id = principal.subject                      # same key queue_story/feed use
    if posted_last_24h(db, user_id, now) >= cap:
        return None
    if has_open_story_job(service.jobs, principal):
        return None                                  # one at a time; never pile up

    entry = None
    for _ in range(MAX_PICKS):
        entry = feed.pick_for_user(db, user_id, now=now)
        if not entry:
            return None                              # library has nothing ready for this user yet
        hit = blocked_by_user(db, entry, blocked_topics)
        if not hit:
            break
        log.info("auto story %s skipped for %s: blocked topic %r", entry["story_asset_id"], user_id, hit)
        feed.mark(db, user_id, entry["trend_id"], "skipped")
        entry = None
    if not entry:
        return None
    try:
        out = queue_story(db, buffer, service, principal, entry["story_asset_id"], None, source="auto")
    except StoryPublishError as exc:
        # Trend stays "used up" for this user (unique feed index); mark it so it is not retried forever.
        log.warning("auto story %s not queued for %s: %s", entry["story_asset_id"], user_id, exc)
        feed.mark(db, user_id, entry["trend_id"], "skipped")
        return None
    except Exception:
        log.exception("auto story queue crashed for %s", user_id)
        feed.mark(db, user_id, entry["trend_id"], "skipped")
        return None
    return {**out, "title": entry.get("trend_title", "Trend story"), "mood": entry.get("mood")}
