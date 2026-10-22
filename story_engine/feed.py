"""story_engine/feed.py — Step 10b: user feed kholte hi library se ready story chuno. Koi research/render nahi.

pick_for_user(db, user_id, vibes=["hype","funny"]) -> feed entry (dict) ya None
Rules:
  - sirf verified + non-expired trend, aur quality_status == "ready" asset
  - ek user ko ek trend sirf ek baar (unique index (user_id, trend_id))
  - ek story_asset sirf ek user ko (atomic claim)  -> dusre user ko same story nahi
  - preferred vibe (mood) wale asset pehle, warna baaki
"""
from datetime import datetime, timezone
from pymongo.errors import DuplicateKeyError
from pymongo import ReturnDocument

from .db import TRENDS, ASSETS, FEED, new_feed_entry


def _claim(db, asset_id, user_id):
    """Atomic: asset tabhi milega jab abhi tak kisi ne claim nahi kiya."""
    return db[ASSETS].find_one_and_update(
        {"_id": asset_id, "claimed_by": None, "quality_status": "ready"},
        {"$set": {"claimed_by": user_id, "claimed_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER)


def pick_for_user(db, user_id, vibes=None, now=None, max_trends=30):
    now = now or datetime.now(timezone.utc)
    vibes = list(vibes or [])
    have = {f["trend_id"] for f in db[FEED].find({"user_id": user_id}, {"trend_id": 1})}
    trends = db[TRENDS].find({"verification_status": "verified", "expires_at": {"$gt": now}}) \
                       .sort("trend_score", -1).limit(max_trends)
    for t in trends:
        if t["trend_id"] in have:
            continue
        # claimed_by None = abhi kisi ko nahi diya gaya; missing field bhi None ki tarah match hota hai
        assets = list(db[ASSETS].find({"trend_id": t["trend_id"], "quality_status": "ready",
                                       "claimed_by": None}))
        assets.sort(key=lambda a: (a["mood"] not in vibes, a["created_at"]))
        for a in assets:
            got = _claim(db, a["_id"], user_id)
            if not got:
                continue                                   # race: kisi aur ne le liya
            entry = new_feed_entry(user_id=user_id, trend_id=t["trend_id"],
                                   story_asset_id=str(got["_id"]),
                                   variant_id=f'{got["angle"]}-{got["layout_id"]}')
            try:
                db[FEED].insert_one(entry)
            except DuplicateKeyError:                      # is user ke paas ye trend pehle se tha
                db[ASSETS].update_one({"_id": got["_id"]}, {"$set": {"claimed_by": None}})
                break
            entry["rendered_image_url"] = got["rendered_image_url"]
            entry["trend_title"] = t["title"]
            return entry
    return None


def mark(db, user_id, trend_id, status):
    """status: seen / posted / skipped"""
    db[FEED].update_one({"user_id": user_id, "trend_id": trend_id}, {"$set": {"status": status}})
