"""Search trends: Google Trends 'trending searches' RSS (reuses trends/sources/google_trends.py).

A trending SEARCH TERM is not a headline, so it is never used as the story text. We only keep a trend that
comes with real news items, pick the headline most of those items agree with, and count as sources only the
publishers whose headline resembles it (otherwise unrelated articles would inflate the publisher count).
Search volume becomes the virality hint (`signal`). Source times are the moment we observed the trend
(`observed=True`), because the feed gives no per-article time.
"""
import logging
import math

from .. import config as cfg
from .base import candidate, similar, utcnow
from .classify import classify
from .grouping import canon_publisher

log = logging.getLogger(__name__)


def traffic_signal(n) -> float:
    """100 searches -> 20, 1K -> 40, 10K -> 60, 100K -> 80, 1M+ -> 100."""
    if not n or n < 1:
        return 25.0
    return max(10.0, min(100.0, (math.log10(n) - 1) * 20))


def headline(news):
    """The headline that most other items agree with (ties: first)."""
    best, best_n = None, -1
    for a in news:
        n = sum(1 for b in news if b is not a and similar(a["title"], b["title"], cfg.SEARCH_MATCH))
        if n > best_n:
            best, best_n = a, n
    return best


def to_candidate(item, now):
    news = item.get("news") or []
    if not news:
        return None
    main = headline(news)
    sources, seen = [], set()
    for a in news:
        if a is not main and not similar(main["title"], a["title"], cfg.SEARCH_MATCH):
            continue
        n = canon_publisher(a["source"])
        if n.lower() in seen:
            continue
        seen.add(n.lower())
        sources.append({"name": n, "url": a["url"], "published_at": item.get("observed_at") or now, "observed": True})
    cat = classify(f"{main['title']} {item.get('title', '')}")
    c = candidate(cat, main["title"], sources, traffic_signal(item.get("approx_traffic_numeric")))
    if c:
        c["search_term"] = item.get("title", "")[:100]
        c["discovered_via"] = ["search_trends"]
    return c


def discover(http=None, now=None, report=None):
    from trends.sources import google_trends          # lazy: keeps this module importable without requests
    now = now or utcnow()
    items = google_trends.fetch(cfg.SEARCH_TRENDS_REGION, http, cfg.SEARCH_TRENDS_MAX)
    if not items and report is not None:
        report.append({"source": "search_trends/feed", "candidates": 0, "error": "empty_or_unavailable"})
    return [c for c in (to_candidate(i, now) for i in items) if c]
