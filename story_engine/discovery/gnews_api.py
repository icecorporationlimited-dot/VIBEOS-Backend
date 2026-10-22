"""GNews official API as an extra news source (optional: needs GNEWS_API_KEY, else it is skipped).

Quota: the free plan is ~100 requests/day and trends/sources/gnews.py shares the same key, so this source
runs at most once per GNEWS_MIN_INTERVAL_MIN minutes (in-process; a restart simply allows one early run).
The key is never logged: failures log only the exception type.
"""
import logging
from datetime import datetime, timedelta, timezone

from .. import config as cfg
from .base import utcnow
from .grouping import group_articles, groups_to_candidates

log = logging.getLogger(__name__)
BASE = "https://gnews.io/api/v4/search"
_last_run = None


def _time(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_articles(payload) -> list:
    out = []
    for a in (payload or {}).get("articles") or []:
        src = (a.get("source") or {}).get("name") or ""
        title = " ".join(str(a.get("title") or "").split())
        url, when = str(a.get("url") or ""), _time(a.get("publishedAt"))
        if src and title and url.startswith("https://") and when:
            out.append({"title": title[:250], "name": src[:80], "url": url, "published_at": when})
    return out


def fetch_category(category, query, key, http=None, now=None):
    if http is None:
        import requests as http
    now = now or utcnow()
    params = {"q": query, "lang": "en", "country": "in", "max": 10, "sortby": "publishedAt",
              "from": (now - timedelta(hours=cfg.MAX_AGE_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ"), "apikey": key}
    r = http.get(BASE, params=params, timeout=15)
    r.raise_for_status()
    return groups_to_candidates(category, group_articles(parse_articles(r.json())), "gnews")


def discover(http=None, now=None, report=None):
    global _last_run
    now = now or utcnow()
    if not cfg.GNEWS_KEY:
        return []
    if _last_run and now - _last_run < timedelta(minutes=cfg.GNEWS_MIN_INTERVAL_MIN):
        return []
    _last_run = now
    out = []
    for cat, q in cfg.GNEWS_QUERIES.items():
        try:
            out += fetch_category(cat, q, cfg.GNEWS_KEY, http, now)
        except Exception as exc:                       # never log the URL: it carries the API key
            log.warning("gnews %s failed: %s", cat, type(exc).__name__)
            if report is not None:
                report.append({"source": f"gnews/{cat}", "candidates": 0, "error": type(exc).__name__})
    return out
