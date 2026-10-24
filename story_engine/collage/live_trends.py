"""Recent trend candidates from RSS feeds, corroborated across independent domains.

Fixes vs the first version:
  * corroboration is fuzzy (token containment / overlap), not "exact same normalised title in 2 feeds", which
    almost never happened between a Google Trends search term and a BBC/TOI headline
  * more default feeds (entertainment + India news), overridable with COLLAGE_TREND_FEEDS
  * sensitive items (death, tragedy, war, politics ...) are dropped: a selfie collage must never carry them
  * every record gets a real `relevance` for the photo category, so the brain can ignore off-topic news
"""
from __future__ import annotations
import os, re, hashlib, logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
import requests

log = logging.getLogger(__name__)

DEFAULT_FEEDS = [
    "https://trends.google.com/trending/rss?geo=IN",
    "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml",
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
]
_STOP = set("a an and are as at be by for from has have in is it its of on or that the this to was were will with "
            "after before over into new says say said vs v live news today how why what who when about out up".split())
_SENSITIVE = re.compile(r"\b(dead|death|dies|died|killed|kill|murder|rape|suicide|attack|blast|bomb|terror|shooting|"
                        r"shot|war|riot|crash|accident|flood|earthquake|fire|hostage|arrest|jail|court|verdict|"
                        r"election|poll|vote|minister|congress|bjp|trump|gaza|israel|ukraine|covid|virus|outbreak)\b", re.I)
_CATEGORY_WORDS = {
    "fashion": "fashion outfit style streetwear sneaker runway collection designer look wedding met gala",
    "music": "music song album concert tour singer band rapper kpop k-pop bts blackpink idol grammy festival lyrics",
    "lifestyle": "travel trip food cafe party birthday friends holiday weekend vacation beach trending viral reel",
    "general": "",
}
_GENZ_WORDS = set(("movie film trailer series season release album song concert tour festival diwali holi eid navratri "
                   "christmas halloween valentine cricket ipl match final worldcup fifa football netflix instagram viral "
                   "trending meme anime game gaming iphone launch fashion").split())


def _feeds():
    raw = os.getenv("COLLAGE_TREND_FEEDS", "").strip()
    return [x.strip() for x in raw.split(",") if x.strip()] or DEFAULT_FEEDS


def _tokens(text):
    return {t for t in re.sub(r"[^a-z0-9 ]", " ", str(text).lower()).split() if len(t) > 1 and t not in _STOP}


def _sensitive(*texts):
    blob = " ".join(str(t) for t in texts if t)
    if _SENSITIVE.search(blob):
        return True
    try:                                                   # reuse the project's stricter reject list when importable
        from story_engine import safety
        return bool(safety.reject_hit(blob))
    except Exception:
        return False


def _matches(a_tokens, b_tokens):
    """A (usually a short search term) is corroborated by B (a headline) if B contains all of A's words,
    or the two overlap strongly."""
    if not a_tokens or not b_tokens:
        return False
    inter = a_tokens & b_tokens
    if a_tokens <= b_tokens and (len(a_tokens) >= 2 or max(len(t) for t in a_tokens) >= 5):
        return True
    return len(inter) >= 2 and len(inter) / len(a_tokens | b_tokens) >= 0.5


def _relevance(category, term, titles, source_count):
    toks = set()
    for t in [term, *titles]:
        toks |= _tokens(t)
    cat = set(_CATEGORY_WORDS.get(category, "").split())
    score = 0.35 + 0.2 * min(source_count, 3) / 3
    if cat & toks:
        score += 0.3
    if _GENZ_WORDS & toks:
        score += 0.2
    return round(min(score, 1.0), 2)


def _fetch(url, now, max_age_hours, timeout):
    out = []
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "VIBEOS-CollageTrendBot/1.1"}); r.raise_for_status()
    for item in ET.fromstring(r.content).findall(".//item"):
        title = (item.findtext("title") or "").strip()
        pub = item.findtext("pubDate")
        try:
            dt = parsedate_to_datetime(pub).astimezone(timezone.utc) if pub else None
        except Exception:
            dt = None
        if not title or not dt or (now - dt).total_seconds() > max_age_hours * 3600:
            continue                                        # no timestamp => not verifiable
        out.append({"title": title, "dt": dt, "url": url, "domain": urlparse(url).netloc.lower(), "tokens": _tokens(title)})
    return out


def scan_verified_trends(category="general", max_age_hours=36, timeout=(3, 4)):
    now = datetime.now(timezone.utc); items = []
    from concurrent.futures import ThreadPoolExecutor
    urls = [u for u in _feeds() if urlparse(u).scheme == "https"]

    def _safe(url):
        try:
            return _fetch(url, now, max_age_hours, timeout)
        except Exception as e:
            log.info("trend feed failed %s: %s", url, e)
            return []
    if urls:
        with ThreadPoolExecutor(max_workers=len(urls)) as pool:
            for got in pool.map(_safe, urls):
                items += got
    verified, seen = [], set()
    for a in items:
        if not a["tokens"] or _sensitive(a["title"]):
            continue
        support = [b for b in items if b["domain"] != a["domain"] and _matches(a["tokens"], b["tokens"])]
        if not support or any(_sensitive(b["title"]) for b in support):
            continue
        domains = {a["domain"], *(b["domain"] for b in support)}
        key = frozenset(a["tokens"])
        if key in seen:
            continue
        seen.add(key)
        rel = _relevance(category, a["title"], [b["title"] for b in support], len(domains))
        verified.append({"term": a["title"], "verified": True, "verification": "independent-domain corroboration",
                         "sources": sorted({a["url"], *(b["url"] for b in support)}),
                         "timestamps": [a["dt"].isoformat(), *(b["dt"].isoformat() for b in support)],
                         "source_count": len(domains), "last_verified_at": now.isoformat(),
                         "relevance": rel, "category": category,
                         "trend_id": hashlib.sha256(a["title"].encode()).hexdigest()[:16]})
    return sorted(verified, key=lambda x: (x["relevance"], x["source_count"]), reverse=True)[:12]
