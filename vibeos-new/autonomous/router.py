"""Turn ready assets into publish routes using the decision engine.

Pure logic, no database or network access, so it is easy to test. The worker
supplies assets plus their cached event detections and applies the result.

Rules:
  * A route is decided once and stored on the asset, so a photo is never
    queued as a photo and later again as a story.
  * Event photos (strong, calendar-matched evidence) stay single photos and
    keep their event window; they are never pulled into a carousel.
  * A Reel (opt-in format) is built from 4-6 good photos of one batch, in upload
    order, and is the first thing carved out of a batch; a carousel then uses
    what is left. Source photos are consumed (never also posted on their own).
  * A rendered Reel (kind="video") is routed as a single "reel" post.
  * A carousel needs 3+ good, unrouted, non-event photos from the same batch
    and is capped at MAX_CAROUSEL. At most one carousel is built per batch.
  * If the chosen format is not allowed by the user's autopilot settings, or a
    story has no story variant, fall back to a photo; if photos are not allowed
    either, the asset is skipped.
"""
from __future__ import annotations

from typing import Any

from autonomous.decision_engine import decide_content

MIN_CAROUSEL = 3
MAX_CAROUSEL = 5
MIN_REEL = 4
MAX_REEL = 6


def _quality(asset: dict) -> float:
    try:
        return float((asset.get("analysis") or {}).get("quality_score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _has_variant(asset: dict, name: str) -> bool:
    return name in ((asset.get("creative") or {}).get("variants") or {})


def _is_event(decision: dict) -> bool:
    return bool(decision.get("event_key"))


def plan_routes(assets: list[dict], detections: dict[Any, dict],
                allowed_formats: list[str], reels_enabled: bool = False) -> list[dict]:
    """Return a list of routes: {"format", "asset_ids", "decision"}.

    A route with format "reel_build" asks the caller to render a Reel from
    asset_ids first; the result is then published as a normal "reel" route.
    reels_enabled  True only when the renderer (FFmpeg) is available.

    assets        ready, unpublished assets (oldest first)
    detections    asset _id -> event detection dict (may be missing/empty)
    allowed_formats  the user's autopilot formats
    """
    allowed = set(allowed_formats)
    routes: list[dict] = []
    handled: set = set()

    # 1. Assets that already have a stored route keep it (stable across passes).
    stored_groups: dict[str, list[dict]] = {}
    for a in assets:
        r = a.get("route")
        if not r:
            continue
        if r["format"] == "reel_source":      # consumed by a Reel, never posted alone
            handled.add(a["_id"])
            continue
        if r["format"] == "carousel":
            stored_groups.setdefault(r.get("group_id") or a["_id"], []).append(a)
        else:
            routes.append({"format": r["format"], "asset_ids": [a["_id"]],
                           "decision": r.get("decision") or {}})
        handled.add(a["_id"])
    for members in stored_groups.values():
        ids = (members[0]["route"].get("asset_ids")) or [m["_id"] for m in members]
        routes.append({"format": "carousel", "asset_ids": list(ids),
                       "decision": members[0]["route"].get("decision") or {}})

    fresh = [a for a in assets if a["_id"] not in handled]

    # A rendered Reel is a single post of its own.
    for a in [a for a in fresh if a.get("kind") == "video"]:
        if "reel" in allowed:
            routes.append({"format": "reel", "asset_ids": [a["_id"]],
                           "decision": {"format": "reel", "content_source": "photo_batch",
                                        "reason": "Rendered Reel from approved photos.",
                                        "requires_fact_check": False,
                                        "auto_publish_allowed": True, "confidence": 0.6}})
        handled.add(a["_id"])
    fresh = [a for a in fresh if a["_id"] not in handled]

    # 2. Per-asset decision (event / story / photo). Carousel is handled below
    #    because it depends on the whole batch.
    singles: list[tuple[dict, dict]] = []
    for a in fresh:
        det = detections.get(a["_id"]) or {}
        decision = decide_content(
            analysis=a.get("analysis"), event_detection=det,
            vibe=(a.get("creative") or {}).get("vibe", "natural"))
        singles.append((a, decision))

    # 3a. Reel: one per batch, from non-event, good-quality assets (story variant
    #     = 9:16 frames). Carved out first; the carousel uses what is left.
    taken: set = set()
    if reels_enabled and "reel" in allowed:
        by_batch_r: dict[Any, list[dict]] = {}
        for a, d in singles:
            if _is_event(d) or _quality(a) < 0.55 or not _has_variant(a, "story"):
                continue
            by_batch_r.setdefault(a.get("batch_id"), []).append(a)
        for batch_id, members in by_batch_r.items():
            if batch_id is None or len(members) < MIN_REEL:
                continue
            best = sorted(members, key=_quality, reverse=True)[:MAX_REEL]
            chosen = sorted(best, key=lambda a: a.get("created_at") or 0)
            ids = [a["_id"] for a in chosen]
            taken.update(ids)
            routes.append({"format": "reel_build", "asset_ids": ids,
                           "decision": {"format": "reel", "content_source": "photo_batch",
                                        "reason": f"{len(ids)} good photos from one batch; "
                                                  "rendered as a photo Reel.",
                                        "requires_fact_check": False,
                                        "auto_publish_allowed": True, "confidence": 0.6}})

    # 3b. Carousel: one per batch, from non-event, good-quality assets.
    carousel_ids: set = set()
    if "carousel" in allowed:
        by_batch: dict[Any, list[tuple[dict, dict]]] = {}
        for a, d in singles:
            if (_is_event(d) or _quality(a) < 0.55 or not _has_variant(a, "post")
                    or a["_id"] in taken):
                continue
            by_batch.setdefault(a.get("batch_id"), []).append((a, d))
        for batch_id, members in by_batch.items():
            if batch_id is None or len(members) < MIN_CAROUSEL:
                continue
            members.sort(key=lambda x: _quality(x[0]), reverse=True)
            chosen = [a for a, _ in members[:MAX_CAROUSEL]]
            engine = decide_content(batch_assets=chosen)
            if engine["format"] != "carousel":
                continue
            ids = [a["_id"] for a in chosen]
            carousel_ids.update(ids)
            routes.append({"format": "carousel", "asset_ids": ids,
                           "decision": engine, "group_id": ids[0]})

    # 4. Remaining assets become photo/story routes with fallbacks.
    for a, d in singles:
        if a["_id"] in carousel_ids or a["_id"] in taken:
            continue
        fmt = d["format"]
        if fmt == "story" and (fmt not in allowed or not _has_variant(a, "story")):
            fmt = "photo"
        if fmt not in allowed:
            fmt = "photo" if "photo" in allowed else None
        if fmt is None:
            continue
        if fmt != d["format"]:
            d = {**d, "format": fmt, "fallback_from": d["format"]}
        routes.append({"format": fmt, "asset_ids": [a["_id"]], "decision": d})
    return routes
