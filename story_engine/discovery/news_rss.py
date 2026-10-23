"""News via Google News RSS search (no API key). One query per category.

Each RSS item carries its publisher in <source>, so grouping similar headlines
gives several DISTINCT publishers per event, which step 3 needs for verification.
Network goes through an injectable `http` (default `requests`) so tests run offline.
"""
import logging
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

from .base import candidate, similar

log = logging.getLogger(__name__)

# category -> search query. Edit freely; add your own niches here.
QUERIES = {
    "cricket": "india cricket match",
    "sports": "sports india",
    "movies": "bollywood movie release",
    "gaming": "gaming release",
    "viral": "viral india",
    "news": "india top news",
}
URL = "https://news.google.com/rss/search?q={q}+when:1d&hl=en-IN&gl=IN&ceid=IN:en"


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


def fetch_category(category: str, query: str, http=None, limit: int = 30):
    import requests
    http = http or requests
    try:
        r = http.get(URL.format(q=quote_plus(query)), timeout=15,
                     headers={"User-Agent": "Mozilla/5.0 (VIBEOS story engine)"})
        r.raise_for_status()
        arts = _parse(r.text)[:limit]
    except Exception as exc:
        log.warning("news_rss %s failed: %s", category, type(exc).__name__)
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
        out += fetch_category(cat, q, http=http)
    return out
