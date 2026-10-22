"""story_engine/db.py — Step 1: collections, indexes and document builders.

`db` is passed in (same pattern as trends/store.py), so this runs in the worker too.
Call ensure_indexes(db) once at startup; it is idempotent.
"""
from datetime import datetime, timedelta, timezone

TRENDS = "se_trends"
ASSETS = "se_story_assets"
FEED = "se_user_story_feed"
LAYOUTS = "se_layouts"
IMAGES = "se_images"

CATEGORIES = {"news", "cricket", "sports", "gaming", "movies", "festival", "viral", "tragedy", "event"}
MOODS = {"hype", "funny", "wholesome", "reflective", "solemn"}
ANGLES = {"surprise", "relatable", "achievement", "tribute", "celebration"}
VERIFICATION = {"pending", "verified", "hold", "rejected"}
QUALITY = {"pending", "ready", "rejected"}
FEED_STATUS = {"unseen", "seen", "posted", "skipped"}


def utcnow():
    return datetime.now(timezone.utc)


def ensure_indexes(db):
    db[TRENDS].create_index("trend_id", unique=True)
    db[TRENDS].create_index([("verification_status", 1), ("trend_score", -1)])
    db[TRENDS].create_index("expires_at", expireAfterSeconds=0)   # Mongo auto-deletes stale trends

    db[ASSETS].create_index([("trend_id", 1), ("mood", 1), ("angle", 1)])
    db[ASSETS].create_index([("quality_status", 1), ("created_at", -1)])
    db[ASSETS].create_index("copy_hash")                           # exact-duplicate check

    db[FEED].create_index([("user_id", 1), ("trend_id", 1)], unique=True)  # one trend per user, ever
    db[FEED].create_index([("user_id", 1), ("status", 1)])
    db[FEED].create_index("story_asset_id")                        # "did another user get this story?"

    db[LAYOUTS].create_index("layout_id", unique=True)
    db[IMAGES].create_index("image_url", unique=True)


def new_trend(*, trend_id, category, title, sources, trend_score=0.0, ttl_hours=24):
    if category not in CATEGORIES:
        raise ValueError(f"bad category: {category}")
    now = utcnow()
    return {
        "trend_id": trend_id, "category": category, "title": title[:300],
        "sources": sources,                      # [{"name","url","published_at"}]
        "verification_status": "pending",        # safety gate decides: verified / hold / rejected
        "first_seen_at": now, "last_verified_at": None,
        "trend_score": float(trend_score),
        "expires_at": now + timedelta(hours=ttl_hours),
    }


def new_asset(*, trend_id, mood, angle, copy, image, layout_id, layout_version, copy_hash):
    if mood not in MOODS or angle not in ANGLES:
        raise ValueError("bad mood/angle")
    return {
        "trend_id": trend_id, "mood": mood, "angle": angle,
        "copy": copy,                            # list of lines
        "image": image,                          # {image_url, source_url, creator, license, attribution_required, verified_at}
        "layout_id": layout_id, "layout_version": layout_version,
        "copy_hash": copy_hash,
        "rendered_image_url": None,
        "quality_status": "pending",             # becomes "ready" only after quality checks
        "created_at": utcnow(),
    }


def new_feed_entry(*, user_id, trend_id, story_asset_id, variant_id):
    return {"user_id": user_id, "trend_id": trend_id, "story_asset_id": story_asset_id,
            "variant_id": variant_id, "status": "unseen", "created_at": utcnow()}


def new_layout(*, layout_id, version, categories, moods, text_slots, image_slots, template_path):
    return {"layout_id": layout_id, "version": version, "categories": categories, "moods": moods,
            "text_slots": text_slots, "image_slots": image_slots,
            "template_path": template_path, "active": True}
