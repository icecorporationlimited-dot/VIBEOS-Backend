"""trends/store.py — MongoDB access for trend items (db is passed in; no settings import,
so this runs in GitHub Actions too)."""
from datetime import datetime, timezone

COLL = "trend_items"


def ensure_indexes(db):
    c = db[COLL]
    c.create_index("key", unique=True)
    # Mongo deletes the doc itself once expires_at passes (TTL monitor runs ~every 60s).
    c.create_index("expires_at", expireAfterSeconds=0)
    c.create_index([("source_type", 1), ("confidence", -1)])


def upsert_items(db, items):
    """Insert new items; refresh existing ones (re-observed trend = new expiry/confidence)."""
    n = 0
    for it in items:
        doc = dict(it)
        key = doc.pop("key")
        db[COLL].update_one(
            {"key": key},
            {"$set": doc, "$setOnInsert": {"first_seen_at": doc["observed_at"]}},
            upsert=True,
        )
        n += 1
    return n


def active_items(db, now=None):
    """All non-expired items (also guards against TTL-monitor lag)."""
    now = now or datetime.now(timezone.utc)
    return list(db[COLL].find({"expires_at": {"$gt": now}}, {"_id": 0}))
