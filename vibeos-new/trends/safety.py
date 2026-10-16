"""Fail-closed safety gate for auto-posted trend Stories.

A trending search is not verified news and is often about something a casual
branded Story must not touch (courts, politics, deaths, crime...). An item is
auto-postable only if ALL of these hold:

  * real context: >= min_sources distinct publishers with https links
  * measurable demand (approx traffic >= min_traffic) and still fresh
  * Latin-script title (the card fonts are not guaranteed for other scripts)
  * no sensitive term (built-in list + the user's /autopilot blocked topics)
    in the trend term, headlines or snippets
  * positively classified into an allowed, harmless category (LLM classifier if
    configured, otherwise a strict keyword allowlist)

Anything uncertain, an unavailable classifier or a classifier error means
"not eligible". Keyword lists are a safety net, not a guarantee: keep
/pause handy and review what gets posted.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

SAFE_CATEGORIES = ("entertainment", "music", "sports", "fashion", "tech", "festival", "lifestyle")

SENSITIVE_TERMS = [
    # politics / government
    "election", "elections", "vote", "voting", "poll", "polls", "minister", "chief minister",
    "prime minister", "president", "parliament", "lok sabha", "rajya sabha", "senate", "congress",
    "bjp", "aap", "mla", "mp", "govt", "government", "policy", "protest", "protests", "rally",
    "trump", "modi", "biden", "kejriwal", "rahul gandhi", "putin", "netanyahu",
    # courts / legal
    "court", "supreme court", "high court", "judge", "chief justice", "justice", "cji", "cjp",
    "verdict", "bail", "hearing", "petition", "lawsuit", "sued", "fir", "chargesheet", "tribunal",
    "sentenced", "convicted", "trial",
    # crime / violence
    "arrest", "arrested", "murder", "murdered", "killed", "kill", "shot", "shooting", "stabbed",
    "rape", "assault", "abuse", "kidnap", "kidnapped", "terror", "terrorist", "terrorism",
    "attack", "blast", "bomb", "riot", "riots", "violence", "lynching", "scam", "fraud", "hacked",
    "leak", "leaked", "mms", "police",
    # death / accidents / disasters / health
    "dies", "died", "death", "dead", "obituary", "passed away", "rip", "funeral", "demise",
    "accident", "crash", "collapse", "earthquake", "flood", "floods", "cyclone", "wildfire",
    "tragedy", "stampede", "explosion", "covid", "outbreak", "virus", "pandemic", "cancer",
    "hospitalised", "hospitalized", "heart attack", "cardiac", "suicide", "self harm",
    # war / geopolitics
    "war", "ceasefire", "missile", "airstrike", "military", "army", "border", "sanctions",
    # religion / communal
    "communal", "hindu", "muslim", "christian", "mosque", "church", "blasphemy", "caste",
    "religion", "religious", "jihad", "conversion",
    # adult / gambling / money advice
    "porn", "nude", "nsfw", "onlyfans", "sex", "betting", "bet", "satta", "lottery", "casino",
    "stock", "stocks", "share price", "sensex", "nifty", "crypto", "bitcoin", "ipo",
    # Hinglish
    "maut", "nidhan", "hatya", "balatkar", "chunav", "sarkar", "adalat", "giraftar", "dhamaka",
    "hamla", "danga",
    # Devanagari
    "चुनाव", "मौत", "हत्या", "अदालत", "गिरफ्तार", "सरकार", "निधन", "दुर्घटना", "हमला",
    "बलात्कार", "दंगा", "आतंकी",
]

SAFE_KEYWORDS = {
    "music": ["song", "songs", "album", "lyrics", "music video", "concert", "single", "rapper"],
    "entertainment": ["movie", "film", "trailer", "teaser", "box office", "ott", "netflix",
                      "web series", "episode", "season", "bigg boss", "actor", "actress",
                      "release date", "prime video", "hotstar"],
    "sports": ["ipl", "cricket", "match", "vs", "world cup", "t20", "odi", "football", "nba",
               "fifa", "tennis", "premier league", "final", "score"],
    "fashion": ["fashion", "outfit", "met gala", "makeup", "style", "runway"],
    "tech": ["iphone", "smartphone", "launch", "gadget", "galaxy", "pixel", "playstation",
             "ai", "android"],
    "festival": ["diwali", "holi", "christmas", "new year", "navratri", "ganesh chaturthi",
                 "valentine", "halloween", "raksha bandhan", "pongal", "onam"],
    "lifestyle": ["recipe", "travel", "vlog", "fitness", "skincare"],
}


def _compile(terms):
    parts = []
    for t in sorted({t.lower() for t in terms if t.strip()}, key=len, reverse=True):
        if t.isascii():
            parts.append(r"(?<!\w)" + re.escape(t) + r"(?!\w)")
        else:
            parts.append(re.escape(t))      # non-Latin: plain substring (stricter)
    return re.compile("|".join(parts), re.IGNORECASE) if parts else None


_SENSITIVE_RE = _compile(SENSITIVE_TERMS)
_SAFE_RES = {cat: _compile(words) for cat, words in SAFE_KEYWORDS.items()}


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _text(item: dict) -> str:
    parts = [item.get("title", "")]
    for n in item.get("news") or []:
        parts += [n.get("title", ""), n.get("snippet", "")]
    return " ".join(p for p in parts if p)


def sensitive_hit(text: str, extra_terms=()):
    """The first sensitive term found in text, else None."""
    m = _SENSITIVE_RE.search(text) if _SENSITIVE_RE else None
    if m:
        return m.group(0).lower()
    extra = _compile([str(t).lstrip("#") for t in extra_terms])
    m = extra.search(text) if extra else None
    return m.group(0).lower() if m else None


def keyword_category(text: str):
    for cat, rx in _SAFE_RES.items():
        if rx and rx.search(text):
            return cat
    return None


def is_latin_title(title: str) -> bool:
    letters = [c for c in title if c.isalpha()]
    return bool(letters) and all(c.isascii() or "LATIN" in _name(c) for c in letters)


def _name(ch):
    import unicodedata
    try:
        return unicodedata.name(ch)
    except ValueError:
        return ""


def _no(reason):
    return {"ok": False, "reason": reason, "category": None}


def assess(item: dict, *, blocked_topics=(), classifier=None, min_sources: int = 2,
           min_traffic: int = 10_000, allowed_categories=SAFE_CATEGORIES,
           min_confidence: float = 0.7, now=None) -> dict:
    """Return {"ok", "reason", "category"}. Never raises; any doubt -> not ok."""
    try:
        now = now or datetime.now(timezone.utc)
        title = str(item.get("title", "")).strip()
        if not title:
            return _no("empty_title")
        if _aware(item["expires_at"]) <= now:
            return _no("expired")
        if not is_latin_title(title):
            return _no("non_latin_title")

        publishers = {str(n.get("source", "")).strip().lower()
                      for n in item.get("news") or []
                      if str(n.get("url", "")).startswith("https://") and n.get("title")}
        publishers.discard("")
        if len(publishers) < min_sources:
            return _no(f"needs_{min_sources}_sources")

        traffic = item.get("approx_traffic_numeric")
        if traffic is None or traffic < min_traffic:
            return _no("traffic_too_low_or_unknown")

        hit = sensitive_hit(_text(item), blocked_topics)
        if hit:
            return _no(f"sensitive:{hit}")

        if classifier is not None:
            try:
                verdict = classifier(item)
            except Exception:
                return _no("classifier_error")
            category = str(verdict.get("category", "")).lower()
            if verdict.get("sensitive") is not False:
                return _no("classifier_sensitive")
            if category not in allowed_categories:
                return _no(f"category_not_allowed:{category or 'none'}")
            if float(verdict.get("confidence", 0)) < min_confidence:
                return _no("classifier_low_confidence")
            return {"ok": True, "reason": "ok", "category": category}

        category = keyword_category(_text(item))
        if category is None or category not in allowed_categories:
            return _no("no_safe_category_match")
        return {"ok": True, "reason": "ok", "category": category}
    except Exception:
        return _no("assess_error")
