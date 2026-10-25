"""Per-photo captions for the collage: clean the vision model's 2-3 word caption, neutral fallbacks as a last resort."""
import re

# Last-resort lines only. Deliberately neutral: they must not claim anything about the photo.
FALLBACKS = ["little things", "random tuesday", "just because", "small moments",
             "the usual", "camera roll", "quiet day", "as it was"]

_BAD = {"none", "unknown", "n a", "na", "null", "photo", "image", "picture", "no caption", "a photo", "the photo"}


_DANGLING = {"a", "an", "the", "at", "in", "on", "of", "with", "and", "to", "by", "for", "from"}


def clean_caption(value):
    """Model text -> 2-3 lowercase words (letters, ' and - only), or "" if unusable."""
    if not isinstance(value, str):
        return ""
    s = re.sub(r"[^a-z' \-]", " ", value.strip().lower())
    words = [w.strip("-'") for w in s.split()]
    words = [w for w in words if w]
    if len(words) < 2 or " ".join(words) in _BAD or any(w in ("unknown", "none", "null") for w in words):
        return ""
    words = words[:3]
    while words and words[-1] in _DANGLING:                      # "golden hour at the beach" -> "golden hour", not "golden hour at"
        words.pop()
    return " ".join(words) if len(words) >= 2 else ""


def pick_caption(caption, used, start=0):
    """Vision caption if usable and not already on this collage, else the next unused neutral fallback.
    `used` is a set shared by the whole collage; `start` rotates which fallback is tried first."""
    c = clean_caption(caption)
    if c and c not in used:
        used.add(c)
        return c
    for i in range(len(FALLBACKS)):
        f = FALLBACKS[(start + i) % len(FALLBACKS)]
        if f not in used:
            used.add(f)
            return f
    return FALLBACKS[start % len(FALLBACKS)]
