"""story_engine/feed.py — Step 10b: user feed kholte hi library se ready story chuno. Koi research/render nahi.

pick_for_user(db, user_id, vibes=None) -> feed entry (dict) ya None
Rules:
  - sirf verified + non-expired trend, aur quality_status == "ready" asset
  - ek user ko ek trend sirf ek baar (unique index (user_id, trend_id))
  - ek story_asset sirf ek user ko (atomic claim)  -> dusre user ko same story nahi
  - vibes: caller ne diye to wahi; warna user ki purani posted/seen stories ke mood se; warna sirf score order
  - preferred mood wale asset pehle, warna baaki
  - har entry me crop (zoom + anchor) hota hai: same story, alag look (variants.py)
"""
from collections import Counter
from datetime import datetime, timezone

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .db import ASSETS, FEED, FEED_STATUS, MOODS, TRENDS, new_feed_entry
from .variants import pick_crop

LEARN_LOOKBACK = 50            # itni purani entries se user ka vibe seekho
# "seen" se "posted" upar nahi jaata ulta: posted ko wapas seen/unseen nahi banate
_RANK = {"unseen": 0, "seen": 1, "skipped": 2, "posted": 3}


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def clean_vibes(vibes) -> list:
    """Sirf jaane-pehchane moods, order same, duplicates hata ke."""
    out = []
    for v in vibes or []:
        v = str(v).strip().lower()
        if v in MOODS and v not in out:
            out.append(v)
    return out


def learn_vibes(db, user_id, top=2) -> list:
    """User ne jin moods ki stories post/see ki, unme se sabse zyada wale. Posted ka wazan seen se zyada."""
    rows = list(db[FEED].find({"user_id": user_id, "status": {"$in": ["posted", "seen"]}}))
    rows.sort(key=lambda r: r.get("created_at") or _now(), reverse=True)
    score = Counter()
    for r in rows[:LEARN_LOOKBACK]:
        a = asset_by_id(db, r.get("story_asset_id")) or {}
        if a.get("mood"):
            score[a["mood"]] += 2 if r["status"] == "posted" else 1
    return [m for m, _ in score.most_common(top)]


def asset_by_id(db, story_asset_id):
    """feed me id string hoti hai, Mongo me ObjectId: dono tarah se dhundo."""
    try:
        from bson import ObjectId
        key = ObjectId(story_asset_id)
    except Exception:
        key = story_asset_id
    return db[ASSETS].find_one({"_id": key})


def _claim(db, asset_id, user_id):
    """Atomic: asset tabhi milega jab abhi tak kisi ne claim nahi kiya."""
    return db[ASSETS].find_one_and_update(
        {"_id": asset_id, "claimed_by": None, "quality_status": "ready"},
        {"$set": {"claimed_by": user_id, "claimed_at": _now()}},
        return_document=ReturnDocument.AFTER)


def _release(db, asset_id):
    db[ASSETS].update_one({"_id": asset_id}, {"$set": {"claimed_by": None, "claimed_at": None}})


def pick_for_user(db, user_id, vibes=None, now=None, max_trends=30):
    now = now or _now()
    vibes = clean_vibes(vibes) or learn_vibes(db, user_id)
    have = {f["trend_id"] for f in db[FEED].find({"user_id": user_id})}
    trends = db[TRENDS].find({"verification_status": "verified", "expires_at": {"$gt": now}})
    from .rank import diversify
    trends = sorted(trends, key=lambda t: t.get("trend_score", 0), reverse=True)
    trends = diversify(trends[:max_trends * 4], max_trends)
    for t in trends:
        if t["trend_id"] in have:
            continue
        # claimed_by None = abhi kisi ko nahi diya gaya; missing field bhi None ki tarah match hota hai
        assets = list(db[ASSETS].find({"trend_id": t["trend_id"], "quality_status": "ready", "claimed_by": None}))
        assets.sort(key=lambda a: (a["mood"] not in vibes, a["created_at"]))
        for a in assets:
            got = _claim(db, a["_id"], user_id)
            if not got:
                continue                                   # race: kisi aur ne le liya
            entry = new_feed_entry(user_id=user_id, trend_id=t["trend_id"],
                                   story_asset_id=str(got["_id"]),
                                   variant_id=f'{got["angle"]}-{got["layout_id"]}')
            entry["crop"] = pick_crop(user_id, got["_id"])
            try:
                db[FEED].insert_one(entry)
            except DuplicateKeyError:                      # is user ke paas ye trend pehle se tha
                _release(db, got["_id"])
                break
            except Exception:                              # koi aur error: claim wapas, phir upar bhejo
                _release(db, got["_id"])
                raise
            entry.pop("_id", None)
            entry.update(rendered_image_url=got["rendered_image_url"], trend_title=t["title"],
                         mood=got["mood"], angle=got["angle"], copy=got["copy"])
            return entry
    return None


def list_feed(db, user_id, status="unseen", now=None, limit=20) -> list:
    """User ki feed entries; jin trends ka waqt nikal gaya (ya delete ho gaye) wo nahi dikhte."""
    now = now or _now()
    out = []
    for f in db[FEED].find({"user_id": user_id, "status": status}):
        t = db[TRENDS].find_one({"trend_id": f["trend_id"]})
        if not t or t.get("verification_status") != "verified" or _aware(t.get("expires_at")) <= now:
            continue
        a = asset_by_id(db, f["story_asset_id"]) or {}
        f.pop("_id", None)
        f.update(rendered_image_url=a.get("rendered_image_url"), trend_title=t["title"],
                 mood=a.get("mood"), angle=a.get("angle"), copy=a.get("copy"))
        out.append(f)
    out.sort(key=lambda f: f.get("created_at") or now, reverse=True)
    return out[:limit]


def get_entry(db, user_id, story_asset_id):
    """Sirf us user ki apni entry; dusre ka asset maangne par None."""
    return db[FEED].find_one({"user_id": user_id, "story_asset_id": str(story_asset_id)})


def mark(db, user_id, trend_id, status) -> bool:
    """status: seen / posted / skipped. Galat status ValueError; peeche ki taraf jump (posted -> seen) nahi hota."""
    if status not in FEED_STATUS or status == "unseen":
        raise ValueError(f"bad status: {status}")
    cur = db[FEED].find_one({"user_id": user_id, "trend_id": trend_id})
    if not cur:
        return False
    if _RANK[status] >= _RANK.get(cur.get("status"), 0):
        db[FEED].update_one({"user_id": user_id, "trend_id": trend_id},
                            {"$set": {"status": status, "updated_at": _now()}})
    return True
