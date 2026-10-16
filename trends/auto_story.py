"""Auto-post a branded Story for a safe, sourced trending topic.

One run per user picks at most ONE topic that passes trends.safety.assess,
renders the card, stores it as a ready Story-only asset and queues a Story job.
The job inherits every normal check (quiet hours, daily limit, min gap, blocked
topics, /pause) and carries `trend_expires_at`: if it has not gone out by then
the worker cancels it, because a stale trend is worse than none.
"""
from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from autonomous.decision_engine import decide_content
from creative_studio.story_templates import render_trend_story
from trends import safety
from trends.schema import make_key
from trends.smart_story import build_story

logger = logging.getLogger(__name__)


@dataclass
class TrendStoryConfig:
    fetch: Callable[[], list]                     # () -> list of trend items (network)
    classifier: Callable[[dict], dict] | None = None
    brand: str = "VIBEOS"
    min_sources: int = 2                          # distinct publishers required
    min_traffic: int = 10_000                     # approx searches required
    max_age_hours: float = 3.0                    # post within this or cancel
    max_per_day: int = 1                          # per user, rolling 24h
    allowed_categories: tuple = safety.SAFE_CATEGORIES
    show_headline: bool = True                    # publisher headline + name on the card
    ai: object | None = None                      # OpenRouterAI-like; enables the smart themed pipeline
    smart: bool = True                            # themed + multi-step verified Stories (needs no AI to run)
    allow_solemn: bool = True                     # condolence Stories (confirmed, >=3 publishers)
    show_sources: bool = True                     # "Source: NDTV, HT" footer on the card


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def posted_last_24h(buffer, tg, now) -> int:
    rows = buffer.assets.find({"telegram_id": str(tg), "source": "trend_story"})
    return len([a for a in rows if _aware(a["created_at"]) > now - timedelta(hours=24)])


def post_one(tg, items, *, cfg: TrendStoryConfig, settings: dict, buffer, service, now=None):
    """Queue one trend Story. Returns a dict describing it, or None if nothing qualified."""
    now = now or datetime.now(timezone.utc)
    if posted_last_24h(buffer, tg, now) >= cfg.max_per_day:
        return None
    ranked = sorted(items, key=lambda i: i.get("approx_traffic_numeric") or 0, reverse=True)
    for item in ranked:
        key = make_key("topic", item.get("title", ""))
        if buffer.assets.find_one({"telegram_id": str(tg), "trend_key": key}):
            continue                                  # this trend was already used
        if cfg.smart:
            built = build_story(item, ai=cfg.ai, brand=cfg.brand,
                                blocked_topics=settings.get("blocked_topics") or [],
                                min_sources=cfg.min_sources, min_traffic=cfg.min_traffic,
                                allow_solemn=cfg.allow_solemn, show_sources=cfg.show_sources, now=now)
            if not built["ok"]:
                logger.info("Trend skipped (%s): %s", built["reason"], str(item.get("title"))[:60])
                continue
            verdict = {"ok": True, "category": built["plan"]["kind"]}
            data = built["jpg"]
        else:
            verdict = safety.assess(
                item, blocked_topics=settings.get("blocked_topics") or [], classifier=cfg.classifier,
                min_sources=cfg.min_sources, min_traffic=cfg.min_traffic,
                allowed_categories=cfg.allowed_categories, now=now)
            if not verdict["ok"]:
                logger.info("Trend skipped (%s): %s", verdict["reason"], str(item.get("title"))[:60])
                continue
        decision = decide_content(trend=item, allow_trend_posts=True)
        if decision["format"] != "story" or not decision["auto_publish_allowed"]:
            continue

        if not cfg.smart:
            with tempfile.TemporaryDirectory(prefix="vibeos_trend_") as tmp:
                path = Path(tmp) / "story.jpg"
                try:
                    render_trend_story(item, path, brand=cfg.brand, show_headline=cfg.show_headline)
                except (OSError, ValueError):
                    logger.exception("Trend card render failed")
                    continue
                data = path.read_bytes()

        publishers = []
        for n in item.get("news") or []:
            if n["source"] not in publishers:
                publishers.append(n["source"])
        meta = {"title": item["title"], "key": key, "category": verdict["category"],
                "publishers": publishers[:5], "decision": decision}
        if cfg.smart:
            meta["verification"] = [x["step"] for x in built["steps"] if x["ok"]]
            meta["ai_source"] = built["plan"]["source"]
        res = buffer.add_trend_story(tg, data, trend_key=key, title=item["title"], meta=meta)
        if res["duplicate"]:
            continue
        asset = res["asset"]
        try:
            job = service.enqueue(tg, [asset["_id"]], "story", scheduled_at=None, source="scheduled")
        except Exception as exc:                       # PublishError / NotConnected / ...
            logger.warning("Trend Story could not be queued: %s", exc)
            buffer.reject(asset["_id"], "trend_enqueue_failed")
            return None
        service.jobs.update_one({"_id": job["_id"]}, {"$set": {
            "trend": meta, "trend_expires_at": now + timedelta(hours=cfg.max_age_hours)}})
        return {"asset_id": asset["_id"], "job_id": job["_id"], **meta}
    return None
