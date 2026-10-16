"""trends/relevance.py — pick trends worth using, and gate them.

Scoring = fit to the user's vibe + confidence + freshness. Buffer/photo-tag matching
is the next step (needs content_buffer tags); the hook is `score(..., photo_tags=)`.
"""
from datetime import datetime, timezone

MIN_SOURCED_CONFIDENCE = 0.5

VIBE_WORDS = {
    "baddie": {"baddie", "confident", "glam", "fashion", "party", "dance", "style"},
    "soft": {"soft", "aesthetic", "cozy", "calm", "love", "romantic", "chill"},
    "moody": {"moody", "cinematic", "dark", "night", "sad", "lofi", "rain"},
    "natural": {"natural", "daily", "vlog", "travel", "food", "life", "casual"},
}


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_blocked(item, blocked_terms):
    text = f"{item.get('title','')} {item.get('use_case') or ''}".lower()
    return any(t.strip().lower() in text for t in (blocked_terms or []) if t.strip())


def usable(item, blocked_terms=(), now=None):
    """Gate: not expired, not blocked, and (if sourced) confident enough."""
    now = now or datetime.now(timezone.utc)
    if _aware(item["expires_at"]) <= now:
        return False
    if is_blocked(item, blocked_terms):
        return False
    if item.get("source_type") != "sourced":  # defensive: never use anything unsourced
        return False
    if item["confidence"] < MIN_SOURCED_CONFIDENCE:
        return False
    return True


def audio_cleared(item):
    """An audio may be recommended for posting only if rights were explicitly verified."""
    return item["kind"] == "audio" and item.get("rights", {}).get("audio_ok") is True


def score(item, vibe, photo_tags=None, now=None):
    now = now or datetime.now(timezone.utc)
    words = VIBE_WORDS.get(vibe, set())
    text = {w for w in f"{item['title']} {item.get('vibe') or ''} {' '.join(item.get('niche_tags', []))}".lower().split()}
    fit = len(words & text) / max(1, len(words))
    if photo_tags:
        fit += 0.5 * len({t.lower() for t in photo_tags} & text)
    age_h = (now - _aware(item["observed_at"])).total_seconds() / 3600
    fresh = max(0.0, 1 - age_h / 48)
    return round(0.4 * fit + 0.4 * item["confidence"] + 0.2 * fresh, 3)


def pick(items, vibe, blocked_terms=(), photo_tags=None, limit=5, now=None):
    """Return {'audio': [...], 'hashtag': [...]} — usable, best first."""
    ok = [i for i in items if usable(i, blocked_terms, now)]
    ranked = sorted(ok, key=lambda i: score(i, vibe, photo_tags, now), reverse=True)
    return {
        "audio": [i for i in ranked if i["kind"] == "audio"][:limit],
        "hashtag": [i for i in ranked if i["kind"] == "hashtag"][:limit],
    }
