"""Run all sources, merge duplicates across sources, return candidates ready for step 3."""
import logging

from . import festival, news_rss
from .base import make_trend_id, similar

log = logging.getLogger(__name__)


def merge(cands):
    """Same event from different sources/categories -> one candidate with combined sources."""
    merged = []
    for c in sorted(cands, key=lambda x: -x["signal"]):
        for m in merged:
            if m["category"] == c["category"] and similar(m["title"], c["title"]):
                have = {s["name"] for s in m["sources"]}
                m["sources"] += [s for s in c["sources"] if s["name"] not in have]
                m["signal"] = max(m["signal"], c["signal"])
                break
        else:
            merged.append(dict(c, sources=list(c["sources"])))
    for m in merged:
        m["trend_id"] = make_trend_id(m["category"], m["title"])
    return merged


def discover_all(http=None, today=None):
    cands = []
    for name, fn in (("news", lambda: news_rss.discover(http=http)),
                     ("festival", lambda: festival.discover(today=today))):
        try:
            cands += fn()
        except Exception:
            log.exception("discovery source %s failed", name)   # one broken source never stops the rest
    return merge(cands)


def save(db, cands, ttl_hours=24):
    """Upsert into se_trends as 'pending'. Existing trends keep their verification status."""
    from ..db import TRENDS, new_trend
    n = 0
    for c in cands:
        doc = new_trend(trend_id=c["trend_id"], category=c["category"], title=c["title"],
                        sources=c["sources"], trend_score=0.0, ttl_hours=ttl_hours)
        fresh = {k: doc[k] for k in ("title", "sources", "expires_at")}
        fresh["signal"] = c["signal"]
        for k in ("title", "sources", "expires_at", "signal"):
            doc.pop(k, None)
        db[TRENDS].update_one({"trend_id": c["trend_id"]},
                              {"$set": fresh, "$setOnInsert": doc}, upsert=True)
        n += 1
    return n
