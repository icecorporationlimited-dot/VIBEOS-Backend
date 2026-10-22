"""Publisher / league / studio RSS + Atom feeds (config.DISCOVERY_FEEDS).

One item = one candidate with ONE source (the feed's publisher). run.merge() then joins the same event
seen in several feeds, which is what gives step 3 its "2+ independent publishers".
Items without a timestamp, older than MAX_AGE_HOURS, or dated in the future are dropped: the safety gate
must never be fed a time we invented. Images in feeds are ignored on purpose (image sources = step 6).
"""
import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .. import config as cfg
from .base import candidate, utcnow
from .classify import classify
from .grouping import aware, canon_publisher

log = logging.getLogger(__name__)


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(el, *names):
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _text(el, *names) -> str:
    c = _child(el, *names)
    return "".join(c.itertext()).strip() if c is not None else ""    # itertext: survives unescaped inline tags


def clean(text) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(text or ""))).split())


def parse_time(text):
    if not text:
        return None
    try:
        return aware(parsedate_to_datetime(text))
    except (TypeError, ValueError):
        pass
    try:
        return aware(datetime.fromisoformat(text.strip().replace("Z", "+00:00")))
    except ValueError:
        return None


def parse_feed(data: bytes) -> list:
    """RSS 2.0 or Atom -> [{title, url, published_at}]. Raises ValueError on hostile/invalid XML."""
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in head:      # no DTDs: blocks entity-expansion attacks
        raise ValueError("DTD/entities not allowed in feeds")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"invalid XML: {exc}") from None
    out = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        link = _text(el, "link")
        if not link:                                      # Atom: <link href="..."/>
            for c in el:
                if _local(c.tag) == "link" and c.get("href") and c.get("rel", "alternate") == "alternate":
                    link = c.get("href").strip()
                    break
        out.append({"title": clean(_text(el, "title")), "url": link,
                    "published_at": parse_time(_text(el, "pubDate", "published", "updated", "date"))})
    return out


def _get(http, url):
    if http is None:
        import requests as http
    r = http.get(url, timeout=cfg.FEED_TIMEOUT,
                 headers={"User-Agent": cfg.HTTP_UA, "Accept": "application/rss+xml, application/atom+xml, text/xml, */*"})
    r.raise_for_status()
    data = r.content
    if len(data) > cfg.FEED_MAX_BYTES:
        raise ValueError("feed too large")
    return data


def _strip_publisher(title, name):
    for sep in (" - ", " | ", " – "):
        if title.endswith(sep + name):
            return title[: -len(sep + name)].strip()
    return title


def signal_for(age_hours, official=False) -> float:
    """Raw hint only: fresher = higher; an owner's own announcement gets a small bump."""
    return min(100.0, 30 + max(0.0, 30 - age_hours * 5) + (10 if official else 0))


def feed_candidates(feed, items, now):
    name = canon_publisher(feed["name"])
    fresh = []
    for it in items:
        when, url, title = it["published_at"], it["url"], _strip_publisher(it["title"], feed["name"])
        if not when or not url.startswith(("http://", "https://")):
            continue
        if not cfg.FEED_MIN_TITLE <= len(title) <= 250:
            continue
        age = (now - when).total_seconds() / 3600
        if age > cfg.MAX_AGE_HOURS or age < -1:           # stale, or a timestamp from the future
            continue
        fresh.append((age, title, url, when))
    fresh.sort(key=lambda r: r[0])
    out = []
    for age, title, url, when in fresh[: cfg.FEED_MAX_ITEMS]:
        cat = feed["category"] if feed["category"] not in cfg.GENERIC_CATEGORIES else classify(title, feed["category"])
        c = candidate(cat, title, [{"name": name, "url": url, "published_at": when}],
                      signal_for(max(age, 0), feed.get("official")), published_at=when)
        if c:
            c["official"] = bool(feed.get("official"))
            c["discovered_via"] = [f"feed:{name}"]
            out.append(c)
    return out


def discover(http=None, now=None, report=None, feeds=None):
    """All enabled feeds. A broken feed is recorded in `report` and skipped."""
    now = now or utcnow()
    out = []
    for feed in feeds if feeds is not None else cfg.DISCOVERY_FEEDS:
        if not feed.get("enabled", True):
            continue
        row = {"source": f"feed:{feed['name']}/{feed['category']}", "candidates": 0, "error": None}
        try:
            out_f = feed_candidates(feed, parse_feed(_get(http, feed["url"])), now)
            row["candidates"] = len(out_f)
            out += out_f
        except Exception as exc:
            row["error"] = type(exc).__name__
            log.warning("feed %s failed: %s", feed["name"], type(exc).__name__)
        if report is not None:
            report.append(row)
    return out
