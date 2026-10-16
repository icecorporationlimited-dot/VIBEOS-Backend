"""GNews adapter (official API) - adds extra publishers to an existing trend.

Why: trends.verify requires several *distinct publishers* whose headlines match
the topic (steps `sources` and `cross_check`). Google Trends RSS usually carries
only 2-3 news items, so good trends fail for lack of evidence. This module looks
the topic up on GNews and appends the extra articles to item["news"].

It only ADDS evidence. It never changes title/traffic/expiry, and verify.py still
runs every check afterwards (relevance match, rumour words, hard-block list), so a
noisy or off-topic result simply fails to count. Needs GNEWS_API_KEY; without it
nothing happens. Free plan is ~100 requests/day, so only the top trends that are
short of publishers are looked up (one request each).

Response shape follows GNews' documented search endpoint and is NOT verified
against the live API from this code base: check one real response first.
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timedelta, timezone

import requests

from trends import verify

log = logging.getLogger(__name__)
BASE = "https://gnews.io/api/v4/search"
MAX_NEWS = 8
MAX_QUERY_CHARS = 180


def search(query: str, api_key: str, *, country: str = "in", lang: str = "en",
           max_results: int = 10, hours: int = 24, session=None, now=None) -> list[dict]:
    """Return recent articles as {title, source, url, snippet}. [] on any failure."""
    query = " ".join(str(query or "").split())[:MAX_QUERY_CHARS]
    if not (query and api_key):
        return []
    http = session or requests
    now = now or datetime.now(timezone.utc)
    params = {
        "q": query, "lang": lang, "country": country.lower(),
        "max": max(1, min(10, int(max_results))), "sortby": "publishedAt",
        "from": (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "apikey": api_key,
    }
    try:
        r = http.get(BASE, params=params, timeout=15)
        r.raise_for_status()
        articles = r.json().get("articles", [])
    except Exception as exc:  # never log the URL: it contains the API key
        log.warning("GNews lookup failed: %s", type(exc).__name__)
        return []
    out = []
    for a in articles if isinstance(articles, list) else []:
        src = (a.get("source") or {}).get("name") or ""
        title = " ".join(str(a.get("title") or "").split())
        url = str(a.get("url") or "")
        if not (src and title and url.startswith("https://")):
            continue
        out.append({"title": title[:250], "source": str(src)[:80], "url": url,
                    "snippet": " ".join(str(a.get("description") or "").split())[:300]})
    return out


def merge_news(item: dict, extra: list[dict], limit: int = MAX_NEWS) -> dict:
    """Return a copy of item with extra articles appended (deduped by publisher+headline)."""
    new = copy.deepcopy(item)
    news = list(new.get("news") or [])
    seen = {(str(n.get("source", "")).lower(), str(n.get("title", "")).lower()) for n in news}
    for a in extra:
        key = (a["source"].lower(), a["title"].lower())
        if key in seen:
            continue
        seen.add(key)
        news.append(a)
        if len(news) >= limit:
            break
    new["news"] = news
    return new


def enrich(items: list[dict], api_key: str, *, country: str = "in", lang: str = "en",
           max_lookups: int = 8, min_publishers: int = 3, session=None, now=None) -> list[dict]:
    """Add GNews articles to the highest-traffic items that lack publishers.

    Returns a new list in the original order; input items are not mutated.
    """
    if not api_key:
        return list(items)
    order = sorted(range(len(items)),
                   key=lambda i: items[i].get("approx_traffic_numeric") or 0, reverse=True)
    out = list(items)
    lookups = 0
    for i in order:
        if lookups >= max_lookups:
            break
        item = items[i]
        if len(verify.publishers(item)) >= min_publishers:
            continue
        lookups += 1
        extra = search(str(item.get("title", "")), api_key, country=country, lang=lang,
                       session=session, now=now)
        if extra:
            merged = merge_news(item, extra)
            merged.setdefault("evidence", {})
            merged["evidence"] = {**merged["evidence"], "news_enriched_by": "gnews"}
            out[i] = merged
    return out
