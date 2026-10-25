"""Step 3c — apply gate + score to every pending/hold trend in MongoDB."""
import logging
import re
from collections import Counter
from datetime import datetime, timezone

from . import config as cfg, scoring, verify
from .db import TRENDS

log = logging.getLogger(__name__)


def process(db, now=None, limit=500) -> dict:
    now = now or datetime.now(timezone.utc)
    stats = {"verified": 0, "hold": 0, "rejected": 0}
    why = Counter()
    rows = db[TRENDS].find({"verification_status": {"$in": ["pending", "hold"]}}).limit(limit)
    for t in rows:
        g = verify.gate(t, now)
        upd = {"verification_status": g["status"], "verify_reasons": g["reasons"],
               "flags": g["flags"], "last_verified_at": now}
        if "solemn" in g["flags"] and g["status"] == "verified":
            upd["category"] = "tragedy"            # writer (step 4) will use solemn rules
        if g["status"] == "verified":
            s = scoring.score({**t, "flags": g["flags"]}, now)
            upd["trend_score"], upd["score_parts"] = s["score"], s["parts"]
        db[TRENDS].update_one({"trend_id": t["trend_id"]}, {"$set": upd})
        stats[g["status"]] += 1
        if g["status"] != "verified":
            for x in g["reasons"][:1]:                       # first reason only, numbers stripped for grouping
                why[re.sub(r"\d+", "N", x)[:60]] += 1
    log.info("verify/score: %s", stats)
    log.info("not-verified reasons: %s", dict(why.most_common(6)))
    return stats


def top_verified(db, limit=10):
    """What step 4 consumes: best verified trends, highest score first."""
    return list(db[TRENDS].find({"verification_status": "verified"}, {"_id": 0})
                .sort("trend_score", -1).limit(limit))


def diversify(trends, limit):
    """Pick `limit` trends (input sorted best-first) without letting one category flood the list.

    Each category gets at most ceil(share * limit) slots (cfg.MAX_CATEGORY_SHARE, cricket lower via
    cfg.CATEGORY_SHARE_OVERRIDE), taken round-robin so the best trend of every category comes first.
    If the caps leave slots empty (few categories available), the remaining best trends fill them.
    """
    import math
    trends = list(trends)
    if limit <= 0:
        return []
    cap = lambda c: max(1, math.ceil(cfg.CATEGORY_SHARE_OVERRIDE.get(c, cfg.MAX_CATEGORY_SHARE) * limit))
    buckets = {}
    for t in trends:
        buckets.setdefault(t.get("category"), []).append(t)
    order = sorted(buckets, key=lambda c: -(buckets[c][0].get("trend_score") or 0))
    picked, taken = [], {c: 0 for c in buckets}
    progress = True
    while len(picked) < limit and progress:
        progress = False
        for c in order:
            if len(picked) >= limit:
                break
            if buckets[c] and taken[c] < cap(c):
                picked.append(buckets[c].pop(0)); taken[c] += 1; progress = True
    if len(picked) < limit:                                   # caps left room: fill with best remaining
        rest = sorted((t for b in buckets.values() for t in b), key=lambda t: -(t.get("trend_score") or 0))
        picked += rest[: limit - len(picked)]
    return sorted(picked, key=lambda t: -(t.get("trend_score") or 0))
