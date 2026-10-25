"""News via Google News RSS search (no API key). One query per category.

Each RSS item carries its publisher in <source>, so grouping similar headlines
gives several DISTINCT publishers per event, which step 3 needs for verification.
Network goes through an injectable `http` (default `requests`) so tests run offline.
"""
import logging
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, quote_plus, urlparse

from .base import candidate, similar

log = logging.getLogger(__name__)

# category -> search query. Edit freely; add your own niches here.
QUERIES = {
    "cricket": "india cricket match",
    "sports": "football OR tennis OR hockey OR f1 india",
    "movies": "bollywood movie release",
    "movies_ott": "web series OR netflix OR ott release india",
    "gaming": "gaming release",
    "viral": "viral india",
    "viral_memes": "meme OR trending OR internet reacts india",
    "news": "india top news",
}
URL = "https://news.google.com/rss/search?q={q}+when:1d&hl=en-IN&gl=IN&ceid=IN:en"
# Fallback when Google blocks the server IP (common on cloud hosts): Bing News RSS, same idea.
BING_URL = "https://www.bing.com/news/search?q={q}&format=rss&setmkt=en-IN&qft=sortbydate%3d%221%22"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9"}


def _parse_bing(xml_text: str):
    out = []
    root = ET.fromstring(xml_text)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = ""
        for c in it:
            if isinstance(c.tag, str) and c.tag.rsplit("}", 1)[-1].lower() == "source":
                pub = (c.text or "").strip()
        real = parse_qs(urlparse(link).query).get("url")      # Bing wraps the real link in ?url=
        if real:
            link = real[0]
        when = None
        try:
            when = parsedate_to_datetime(it.findtext("pubDate"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
        if title and link.startswith("http") and pub:
            out.append({"title": title, "name": pub, "url": link, "published_at": when})
    return out


def _parse(xml_text: str):
    out = []
    root = ET.fromstring(xml_text)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src = it.find("source")
        pub = (src.text or "").strip() if src is not None else ""
        if pub and title.endswith(" - " + pub):          # Google appends " - Publisher"
            title = title[: -len(pub) - 3].strip()
        when = None
        try:
            when = parsedate_to_datetime(it.findtext("pubDate"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
        if title and link.startswith("http") and pub:
            out.append({"title": title, "name": pub, "url": link, "published_at": when})
    return out


def _get_articles(category, query, http, limit):
    """Google News first, Bing News if Google fails. Logs the real HTTP status."""
    attempts = (("google", URL, _parse), ("bing", BING_URL, _parse_bing))
    for name, tmpl, parser in attempts:
        try:
            r = http.get(tmpl.format(q=quote_plus(query)), timeout=15, headers=_HEADERS)
            r.raise_for_status()
            arts = parser(r.text)[:limit]
            if arts:
                return arts
            log.warning("news_rss %s [%s]: 0 items", category, name)
        except Exception as exc:
            code = getattr(getattr(exc, "response", None), "status_code", "?")
            log.warning("news_rss %s [%s] failed: %s status=%s", category, name, type(exc).__name__, code)
    return []


def fetch_category(category: str, query: str, http=None, limit: int = 30):
    import requests
    http = http or requests
    arts = _get_articles(category, query, http, limit)
    if not arts:
        return []
    # group similar headlines into one event; more publishers + earlier rank = stronger signal
    groups = []
    for rank, a in enumerate(arts):
        for g in groups:
            if similar(g["title"], a["title"]):
                if a["name"] not in {s["name"] for s in g["sources"]}:
                    g["sources"].append({**{k: a[k] for k in ("name", "url", "published_at")}, "title": str(a.get("title") or "")[:300]})
                break
        else:
            groups.append({"title": a["title"], "rank": rank,
                           "sources": [{**{k: a[k] for k in ("name", "url", "published_at")}, "title": str(a.get("title") or "")[:300]}]})
    out = []
    for g in groups:
        signal = min(100, len(g["sources"]) * 25 + max(0, 30 - g["rank"]))
        c = candidate(category, g["title"], g["sources"], signal)
        if c:
            out.append(c)
    return out


def discover(http=None, queries=None):
    out = []
    for cat, q in (queries or QUERIES).items():
        out += fetch_category(cat.split("_")[0], q, http=http)     # "movies_ott" -> category "movies"
    return out
