"""Step 2 runner: every source -> one common candidate format -> merged events -> saved as 'pending'.

Sources (config.DISCOVERY_ENABLED switches each one):
  news_rss       Google News RSS search per category
  feeds          publisher / league / studio RSS+Atom feeds (config.DISCOVERY_FEEDS)
  search_trends  Google Trends trending searches that carry real news items
  gnews          GNews official API (optional key)
  festival       event_calendar.json (no network)
A broken source is logged and skipped; it never stops the others.
Trend sources only: image sources belong to step 6 and are never read here.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from .. import config as cfg
from . import festival, feeds, gnews_api, news_rss, search_trends
from .base import make_trend_id, similar, utcnow
from .grouping import canon_publisher

log = logging.getLogger(__name__)


def _compatible(a, b) -> bool:
    ca, cb = a["category"], b["category"]
    return ca == cb or ca in cfg.GENERIC_CATEGORIES or cb in cfg.GENERIC_CATEGORIES


def _normalize(c):
    c = dict(c, sources=[dict(s, name=canon_publisher(s["name"])) for s in c["sources"]])
    c.setdefault("discovered_via", [])
    return c


def merge(cands):
    """Same event from different sources -> one candidate with every distinct publisher.

    A specific category (cricket) beats a generic one (news). More publishers = stronger raw signal.
    """
    merged = []
    for c in sorted((_normalize(x) for x in cands), key=lambda x: -x["signal"]):
        for m in merged:
            if _compatible(m, c) and similar(m["title"], c["title"]):
                have = {s["name"].lower() for s in m["sources"]}
                added = [s for s in c["sources"] if s["name"].lower() not in have]
                m["sources"] += added
                m["signal"] = min(100.0, max(m["signal"], c["signal"]) + 15 * len(added))
                if m["category"] in cfg.GENERIC_CATEGORIES and c["category"] not in cfg.GENERIC_CATEGORIES:
                    m["category"] = c["category"]
                m["discovered_via"] = sorted(set(m["discovered_via"]) | set(c["discovered_via"]))
                m["official"] = bool(m.get("official") or c.get("official"))
                break
        else:
            merged.append(dict(c, sources=list(c["sources"])))
    for m in merged:
        m["trend_id"] = make_trend_id(m["category"], m["title"])
    return merged


def _registry(today):
    return [
        ("news_rss", lambda http, now, rep: news_rss.discover(http=http)),
        ("feeds", lambda http, now, rep: feeds.discover(http=http, now=now, report=rep)),
        ("search_trends", lambda http, now, rep: search_trends.discover(http=http, now=now, report=rep)),
        ("gnews", lambda http, now, rep: gnews_api.discover(http=http, now=now, report=rep)),
        ("festival", lambda http, now, rep: festival.discover(today=today)),
    ]


def discover_report(http=None, today=None, now=None):
    """-> (merged candidates, per-source health report). Network goes through `http` (tests inject a fake)."""
    now = now or utcnow()
    report, cands = [], []
    for name, fn in _registry(today):
        if not cfg.DISCOVERY_ENABLED.get(name, True):
            continue
        t0, rows, err = time.time(), [], None
        try:
            rows = fn(http, now, report) or []
        except Exception as exc:
            err = type(exc).__name__
            log.exception("discovery source %s failed", name)
        report.append({"source": name, "candidates": len(rows), "error": err, "seconds": round(time.time() - t0, 2)})
        cands += rows
    merged = merge(cands)
    log.info("discovery: %d candidates -> %d events | %s", len(cands), len(merged),
             ", ".join(f"{r['source']}={r['candidates']}{'!' if r['error'] else ''}" for r in report
                       if not r["source"].startswith("feed:")))
    bad = [r["source"] for r in report if r["error"]]
    if bad:
        log.warning("discovery sources with errors: %s", ", ".join(bad))
    return merged, report


def discover_all(http=None, today=None, now=None):
    """Same return type as before (list of candidates), so run_loop / run_pipeline need no change."""
    return discover_report(http, today, now)[0]


def _union_sources(old, new):
    """One entry per publisher; the newest timestamp wins, so freshness reflects the latest report."""
    by = {}
    for s in list(old or []) + list(new or []):
        k = s["name"].lower()
        cur = by.get(k)
        a, b = (cur or {}).get("published_at"), s.get("published_at")
        if cur is None or (b and (not a or _naive(b) > _naive(a))):
            by[k] = s
    return list(by.values())


def _naive(dt):
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def save(db, cands, ttl_hours=24):
    """Upsert into se_trends as 'pending'.

    An event we already track (same or similar headline, still unexpired) keeps its trend_id, title and
    status; only its publishers (union), signal and expiry are refreshed. So a trend on 'hold' can reach
    the publisher count over several runs, and one event never becomes two trends.
    """
    from ..db import TRENDS, new_trend
    now = datetime.now(timezone.utc)
    existing = list(db[TRENDS].find({"expires_at": {"$gt": now}}, {"trend_id": 1, "title": 1, "sources": 1,
                                                                    "signal": 1, "discovered_via": 1}))
    n = 0
    for c in cands:
        match = next((e for e in existing if e["trend_id"] == c["trend_id"]), None) \
            or next((e for e in existing if similar(e.get("title", ""), c["title"])), None)
        if match:
            sources = _union_sources(match.get("sources"), c["sources"])
            db[TRENDS].update_one({"trend_id": match["trend_id"]}, {"$set": {
                "sources": sources, "signal": max(float(match.get("signal") or 0), c["signal"]),
                "expires_at": now + timedelta(hours=ttl_hours),
                "discovered_via": sorted(set(match.get("discovered_via") or []) | set(c.get("discovered_via") or []))}})
            match["sources"] = sources
        else:
            doc = new_trend(trend_id=c["trend_id"], category=c["category"], title=c["title"],
                            sources=c["sources"], trend_score=0.0, ttl_hours=ttl_hours)
            fresh = {k: doc.pop(k) for k in ("title", "sources", "expires_at")}
            fresh.update(signal=c["signal"], discovered_via=c.get("discovered_via", []),
                         official=bool(c.get("official")))
            db[TRENDS].update_one({"trend_id": c["trend_id"]}, {"$set": fresh, "$setOnInsert": doc}, upsert=True)
            existing.append({"trend_id": c["trend_id"], "title": c["title"], "sources": c["sources"],
                             "signal": c["signal"], "discovered_via": c.get("discovered_via", [])})
        n += 1
    return n
