"""Google Trends trending-search RSS adapter (best-effort public feed).

Feed results are trend signals, not verified news. Each trend usually carries a
few news items (headline, publisher, URL) which give the context and the real
sources; those are parsed into item["news"].

The feed uses an XML namespace for its own fields, so elements are matched by
local name (namespace-agnostic). The field layout is parsed from the feed's
documented shape and is NOT verified against the live feed from this code base:
check one real response before relying on it.
"""
from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)
FEED = "https://trends.google.com/trending/rss?geo={geo}"
MAX_NEWS = 5


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child_text(parent, name: str) -> str:
    for el in parent.iter():
        if el is not parent and _local(el.tag) == name:
            return (el.text or "").strip()
    return ""


def _clean_text(text: str) -> str:
    """Feed text may carry HTML tags/entities; the card must show plain text."""
    return html.unescape(re.sub(r"<[^>]+>", "", html.unescape(text or ""))).strip()


def parse_traffic(text: str):
    """'200+' -> 200, '2K+' -> 2000, '1M+' -> 1_000_000. None if unknown."""
    m = re.match(r"^\s*([0-9][0-9,\.]*)\s*([kKmM]?)\s*\+?\s*$", text or "")
    if not m:
        return None
    try:
        number = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    mult = {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2).lower()]
    return int(number * mult)


def _news_items(item) -> list[dict]:
    out, seen = [], set()
    for node in item.iter():
        if _local(node.tag) != "news_item":
            continue
        url = _child_text(node, "news_item_url")
        title = _clean_text(_child_text(node, "news_item_title"))
        source = _clean_text(_child_text(node, "news_item_source"))
        snippet = _clean_text(_child_text(node, "news_item_snippet"))
        if not (title and source and url.startswith("https://")):
            continue
        if (source.lower(), title.lower()) in seen:
            continue
        seen.add((source.lower(), title.lower()))
        out.append({"title": title[:250], "source": source[:80], "url": url,
                    "snippet": snippet[:300]})
        if len(out) >= MAX_NEWS:
            break
    return out


def fetch(region: str = "IN", session=None, limit: int = 20) -> list[dict]:
    http = session or requests
    region = (region or "IN").upper()
    url = FEED.format(geo=quote(region))
    try:
        response = http.get(url, timeout=15, headers={"User-Agent": "VibeOS-TrendReader/1.0"})
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except Exception as exc:
        log.warning("Google Trends feed unavailable: %s", type(exc).__name__)
        return []

    now = datetime.now(timezone.utc)
    out = []
    for item in root.findall(".//item")[:max(1, min(50, int(limit)))]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        approx = _child_text(item, "approx_traffic")
        if not title or not link.startswith("https://"):
            continue
        out.append({
            "kind": "topic", "title": title[:250], "source_type": "sourced",
            "source_name": "google_trends_rss", "source_url": link,
            "evidence": {"approx_traffic": approx, "region": region},
            "confidence": 0.62, "observed_at": now,
            "expires_at": now + timedelta(hours=6),
            # Approximate search volume; it is not a truth score.
            "approx_traffic_numeric": parse_traffic(approx),
            "news": _news_items(item),
            "needs_fact_check": True,
        })
    return out
