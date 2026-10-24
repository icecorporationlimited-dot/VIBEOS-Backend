"""Step 2b — Confirm. Look up 'hold' trends that lack publishers and add the publishers that really cover them.

Why: discovery groups headlines inside each feed/query, so one real event often ends up as several
single-publisher trends (438 of ~490 were 'only 1 publisher'). Here each such trend's headline is searched on
Google News RSS (Bing as fallback, same helper as news_rss; no API key, no quota). A result counts as another
source only if it is a DIFFERENT publisher, is fresh, and its own headline clearly matches (keyword overlap AND
several shared keywords). This module only ADDS evidence; verify.py then re-runs every check (publisher count,
trusted count, disputes, numbers, rumour words, freshness), so a noisy match simply fails to verify.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from .. import config as cfg
from .. import safety, verify
from ..db import TRENDS
from .base import similar, tokens
from .grouping import aware, canon_publisher
from .news_rss import _get_articles
from .run import _union_sources

log = logging.getLogger(__name__)


def _need(title) -> int:
    solemn = bool(verify._has(title, cfg.DEATH_WORDS))
    return cfg.MIN_PUBLISHERS_SOLEMN if solemn else cfg.MIN_PUBLISHERS


def _same_event(title, other) -> bool:
    return similar(title, other, cfg.CONFIRM_MATCH) and len(tokens(title) & tokens(other)) >= cfg.CONFIRM_MIN_SHARED


def matching_sources(trend, articles, now) -> list:
    """Articles (from news_rss._get_articles) that are another publisher's report of the same event."""
    have = {n.lower() for n in verify.publishers(trend)}
    fresh_after = now - timedelta(hours=cfg.MAX_AGE_HOURS)
    out = []
    for a in articles:
        name = canon_publisher(a.get("name"))
        when = aware(a.get("published_at"))
        if not name or name.lower() in have or not when or when < fresh_after:
            continue
        if not _same_event(trend.get("title", ""), a.get("title", "")):
            continue
        have.add(name.lower())
        out.append({"name": name, "url": a["url"], "published_at": when, "title": str(a.get("title") or "")[:300]})
    return out


def _candidates(db, now, limit):
    cutoff = now - timedelta(minutes=cfg.CONFIRM_RECHECK_MIN)
    rows = []
    for t in db[TRENDS].find({"verification_status": {"$in": ["hold", "pending"]}}):
        if t.get("category") == "festival":
            continue
        title = t.get("title", "")
        if len(verify.publishers(t)) >= _need(title) or safety.reject_hit(title):
            continue
        checked = aware(t.get("confirm_checked_at"))
        if checked and checked > cutoff:
            continue
        age = verify.age_hours(t, now)
        if age is not None and age > cfg.MAX_AGE_HOURS:
            continue
        rows.append((checked is not None, -float(t.get("signal") or 0), t))     # never-checked first, then strongest
    rows.sort(key=lambda r: (r[0], r[1]))
    return [r[2] for r in rows[:limit]]


def process(db, http=None, now=None, sleep=None, limit=None) -> dict:
    stats = {"looked_up": 0, "enriched": 0, "sources_added": 0, "failed": 0}
    if not cfg.CONFIRM_ENABLED:
        return stats
    import requests
    http = http or requests
    now = now or datetime.now(timezone.utc)
    sleep = sleep or time.sleep
    fails = 0
    for i, t in enumerate(_candidates(db, now, limit or cfg.CONFIRM_MAX_LOOKUPS)):
        if i:
            sleep(cfg.CONFIRM_DELAY)
        stats["looked_up"] += 1
        arts = _get_articles(t.get("category", "news"), t["title"][:120], http, 20)
        upd = {"confirm_checked_at": now}
        if not arts:
            stats["failed"] += 1
            fails += 1
            db[TRENDS].update_one({"trend_id": t["trend_id"]}, {"$set": upd})
            if fails >= cfg.CONFIRM_MAX_FAILS:
                log.warning("confirm: %d lookups in a row returned nothing; stopping this pass", fails)
                break
            continue
        fails = 0
        extra = matching_sources(t, arts, now)
        if extra:
            upd["sources"] = _union_sources(t.get("sources"), extra)
            upd["discovered_via"] = sorted(set(t.get("discovered_via") or []) | {"confirm"})
            stats["enriched"] += 1
            stats["sources_added"] += len(extra)
        db[TRENDS].update_one({"trend_id": t["trend_id"]}, {"$set": upd})
    return stats
