"""Human-Likeness Quality Check: "Would a real person post this?"

Five checks, each 20 points. A story ships only at >= 70. Rendered previews
should still be reviewed by the human before publishing - this score is a
gate, not a guarantee.
"""
from __future__ import annotations

import re

NEWSY = ("breaking", "announces", "reportedly", "sources say", "in the news",
         "trending now", "record holder", "source:", "via ")
DEVI_RE = re.compile(r"[\u0900-\u097F]")


def review(caption, layout, info, recent=()) -> dict:
    checks, notes = {}, []

    # 1) natural caption: a believable human reaction, not a headline
    text = str(caption or "").strip()
    words = len(text.split())
    natural = 0 < words <= 14 and not text.endswith("...") and not DEVI_RE.search(text)
    if text and text.isupper() and layout.get("persona") != "chaotic_meme":
        natural = False
    if any(w in text.lower() for w in NEWSY):
        natural = False
    checks["natural_caption"] = natural
    if not natural:
        notes.append("caption reads like a headline, not a thought")

    # 2) photo relevance: mood is known and the persona matches it
    persona, mood = layout.get("persona"), info.get("mood", "everyday")
    from vibe_engine.personalities import MOOD_PERSONA
    relevant = mood in MOOD_PERSONA and (persona == MOOD_PERSONA.get(mood) or persona == "chaotic_meme")
    checks["photo_relevance"] = relevant
    if not relevant:
        notes.append("personality does not fit the photo mood")

    # 3) visual variety: signature not in recent history
    sig = layout.get("signature", "")
    variety = sig.rsplit("|", 1)[0] not in {str(r).rsplit("|", 1)[0] for r in recent if r}
    checks["visual_variety"] = variety
    if not variety:
        notes.append("layout repeats a recent story (anti-template triggered)")

    # 4) human placement: text sits in a calm area, faces untouched
    placement_ok = layout.get("placement") in ("bottom_center",) or layout.get("scrim", 0) >= 40
    face_safe = not (layout.get("placement", "").startswith("mid") and info.get("faces"))
    checks["human_placement"] = bool(placement_ok and face_safe)
    if not checks["human_placement"]:
        notes.append("caption placement risks covering a face or busy area")

    # 5) no news-card signals
    checks["no_news_card"] = not any(w in text.lower() for w in ("source", "via", "stats", "searches"))
    if not checks["no_news_card"]:
        notes.append("news-card furniture detected in caption")

    score = sum(20 for ok in checks.values() if ok)
    return {"score": score, "passed": checks, "notes": notes, "ok": score >= 70,
            "note": "score gates publishing; still review the rendered preview"}
