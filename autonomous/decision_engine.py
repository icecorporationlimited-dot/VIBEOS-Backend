"""Conservative decision layer for routing assets to post/story/reel/carousel.

This is a deterministic policy layer, not a vision model. It consumes image
analysis/event labels produced elsewhere and never guesses that a normal photo
is festival-related. Call it after photo analysis and before creating a publish
job. Unknown/low-confidence signals fall back to a normal photo post.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

VALID_VIBES = {"baddie", "soft", "moody", "natural"}
# Format-routing hint only; this must never decide whether a photo is accepted.
MIN_CAROUSEL_QUALITY = 0.35


def _clamp01(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def decide_content(*, analysis: dict | None = None,
                    event_detection: dict | None = None,
                    trend: dict | None = None,
                    batch_assets: list[dict] | None = None,
                    vibe: str = "natural",
                    allow_trend_posts: bool = False) -> dict[str, Any]:
    """Choose a safe content route and explain it.

    Inputs are intentionally explicit. `trend` must contain source_url,
    source_name and confidence to qualify. Political/current-affairs trends are
    not auto-published by this function; they are routed to a branded Story
    draft for fact-checking unless the caller has a separate verified policy.
    """
    analysis = analysis if isinstance(analysis, dict) else {}
    event = event_detection if isinstance(event_detection, dict) else {}
    assets = batch_assets or []
    confidence = _clamp01(event.get("confidence"))
    is_event = bool(event.get("event_related")) and confidence >= 0.78 and bool(event.get("event_key"))

    # Trend content is separate from personal-photo content. It must be sourced
    # and fresh; source/platform popularity is never treated as IG audio rights.
    if trend:
        sourced = bool(trend.get("source_url") and trend.get("source_name"))
        trend_conf = _clamp01(trend.get("confidence"))
        expires = trend.get("expires_at")
        fresh = True
        if isinstance(expires, datetime):
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            fresh = expires > datetime.now(timezone.utc)
        if sourced and trend_conf >= 0.60 and fresh:
            return {
                "format": "story", "content_source": "verified_trend_candidate",
                "event_key": None, "trend_title": str(trend.get("title", ""))[:200],
                "requires_fact_check": True, "auto_publish_allowed": bool(allow_trend_posts),
                "reason": "Fresh sourced trend candidate; make a branded Story draft and verify claims before publishing.",
                "confidence": round(trend_conf, 3),
            }
        return {"format": "photo", "content_source": "personal_photo",
                "requires_fact_check": False, "auto_publish_allowed": True,
                "reason": "Trend evidence was missing, weak, or expired; use the normal content calendar.",
                "confidence": 0.0}

    # Multiple sufficiently different assets are a carousel candidate, not a
    # Reel automatically: stills alone need rendering to become a video.
    if len(assets) >= 2:
        good = [a for a in assets if _clamp01((a.get("analysis") or {}).get("quality_score", 0)) >= MIN_CAROUSEL_QUALITY]
        if len(good) >= 2:
            return {"format": "carousel", "content_source": "photo_batch",
                    "event_key": event.get("event_key") if is_event else None,
                    "requires_fact_check": False, "auto_publish_allowed": True,
                    "reason": "At least two usable, distinct batch assets; carousel is preferable to inventing a Reel.",
                    "confidence": 0.72}

    # A festival window is only selected if image understanding found strong
    # visible evidence and the event has a calendar match upstream.
    if is_event:
        return {"format": "photo", "content_source": "occasion_matched_photo",
                "event_key": str(event["event_key"]), "requires_fact_check": False,
                "auto_publish_allowed": True,
                "reason": "Strong visual occasion evidence; the planner may reserve an event-window slot.",
                "confidence": round(confidence, 3)}

    summary = str(event.get("visual_summary", "")).lower()
    # Stories are better suited to casual, behind-the-scenes snapshots, but do
    # not force a story based on pixel metrics alone. Only use explicit labels.
    content_type = str(event.get("content_type", "general")).lower()
    if content_type in {"behind_the_scenes", "casual_update", "daily_life"}:
        fmt, reason = "story", "The semantic content label suggests a casual update; no event was inferred."
    else:
        fmt, reason = "photo", "No strong occasion match; schedule as normal evergreen content."
    return {"format": fmt, "content_source": "personal_photo", "event_key": None,
            "requires_fact_check": False, "auto_publish_allowed": True,
            "reason": reason, "visual_summary": summary[:300], "vibe": vibe if vibe in VALID_VIBES else "natural",
            "confidence": 0.65 if content_type != "general" else 0.5}


def decide_trend_action(item: dict, *, brand_topics: list[str] | None = None,
                        blocked_terms: list[str] | None = None) -> dict[str, Any]:
    """Gate a trend before story generation; never auto-post unsourced claims."""
    title = str(item.get("title", "")).strip()
    combined = (title + " " + str(item.get("summary", ""))).lower()
    blocked = [x.lower().strip() for x in (blocked_terms or []) if x.strip()]
    if not title or not item.get("source_url") or not item.get("source_name"):
        return {"eligible": False, "action": "ignore", "reason": "Missing source/title."}
    if any(term in combined for term in blocked):
        return {"eligible": False, "action": "ignore", "reason": "Matches a configured blocked topic."}
    conf = _clamp01(item.get("confidence"))
    if conf < 0.60:
        return {"eligible": False, "action": "ignore", "reason": "Source confidence below threshold."}
    topics = [x.lower().strip() for x in (brand_topics or []) if x.strip()]
    topic_fit = not topics or any(t in combined for t in topics)
    if not topic_fit:
        return {"eligible": False, "action": "ignore", "reason": "Trend does not fit configured brand topics."}
    return {"eligible": True, "action": "draft_story", "requires_fact_check": True,
            "auto_publish": False,
            "reason": "Sourced candidate fits brand topics; generate a neutral draft, verify the facts, then publish."}
