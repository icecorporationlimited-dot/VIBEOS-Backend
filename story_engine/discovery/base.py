"""Common candidate shape + helpers shared by every discovery source.

candidate = {
  "category": "cricket",
  "title": "India beat Australia in 3rd ODI",
  "sources": [{"name": "NDTV", "url": "https://...", "published_at": datetime}],
  "signal": 0-100,          # raw virality hint from the source (rank/traffic), NOT a verdict
  "published_at": datetime, # earliest source time
}
Verification and scoring happen in later steps; discovery only collects.
"""
import hashlib
import re
from datetime import datetime, timezone

_STOP = {"the", "a", "an", "in", "on", "of", "to", "for", "and", "at", "vs", "v", "is", "are", "as", "by",
         "with", "after", "from", "live", "update", "updates", "news"}


def utcnow():
    return datetime.now(timezone.utc)


_NUM = {"first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th", "fifth": "5th"}


def _norm(w: str) -> str:
    w = _NUM.get(w, w)
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]                                   # beats -> beat, wins -> win
    return w


def tokens(title: str) -> set:
    words = (_norm(w) for w in re.findall(r"[\w']+", (title or "").lower()))
    return {w for w in words if w not in _STOP and len(w) > 1}


def similar(a: str, b: str, threshold: float = 0.45) -> bool:
    """Jaccard overlap of keywords: 'India beat Australia' ~ 'India beats Australia in ODI'."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= threshold


def make_trend_id(category: str, title: str) -> str:
    key = " ".join(sorted(tokens(title)))
    return hashlib.sha1(f"{category}|{key}".encode("utf-8")).hexdigest()[:16]


def candidate(category, title, sources, signal=50, published_at=None):
    title = " ".join((title or "").split())
    if not title or not sources:
        return None
    times = [s["published_at"] for s in sources if s.get("published_at")]
    return {"category": category, "title": title[:300], "sources": sources,
            "signal": max(0, min(100, float(signal))),
            "published_at": published_at or (min(times) if times else utcnow())}
