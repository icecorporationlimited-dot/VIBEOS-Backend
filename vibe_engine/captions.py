"""Human Reaction Generator: small thoughts, not headlines.

Generates caption alternatives with DIFFERENT intents (funny, relatable,
curious, sarcastic, aesthetic) - not synonyms of the same sentence. With AI it
reads the photo mood + optional user note; without AI it falls back to mood-
aware canned thoughts per personality.
"""
from __future__ import annotations

import hashlib
import json
import re

from vibe_engine.personalities import PERSONALITIES

INTENTS = ("funny", "relatable", "curious", "sarcastic", "aesthetic")
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
TAG_RE = re.compile(r"[#@]\w+")
EMOJI_RE = re.compile("([\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF][\uFE0F\u200D]*)")

FALLBACK = {
    "casual_genz": {
        "night": ["ok why is this view so unfair", "late night vibes, no thoughts head empty",
                  "sab so rahe hain and i am here 😭"],
        "sunset": ["sunsets really said free therapy", "golden hour hit different today",
                   "wait 2 min, bas picture khichani thi"],
        "golden": ["this light is doing things to me", "no filter, bas perfect timing"],
        "green": ["finally touched some grass", "parks > people, proved"],
        "blue_moody": ["main character energy but make it blue", "moody skies matching mood"],
        "clean": ["clean slate kinda day", "some days just feel lighter"],
        "everyday": ["not me seeing this for the 47th time", "regular day, irregular photo",
                     "saved this before posting, it's that good"],
    },
    "soft_aesthetic": {
        "night": ["some nights just ask for silence.", "the moon was enough tonight."],
        "sunset": ["some days just feel different.", "the sky did most of the talking."],
        "golden": ["soft light, softer mood.", "keeping this one for myself."],
        "green": ["quiet places, loud heart.", "green is a feeling too."],
        "blue_moody": ["blue, but the calm kind.", "staying soft about it."],
        "clean": ["a clear day, a clear mind.", "less, but softer."],
        "everyday": ["a small moment worth keeping.", "documenting the ordinary."],
    },
    "chaotic_meme": {
        "night": ["3 AM BEHAVIOUR 💀", "THE NIGHT SHIFT NOBODY ASKED FOR"],
        "sunset": ["THE SKY IS SHOWING OFF AGAIN", "SUNSET SAID LOOK AT ME. I LOOKED."],
        "golden": ["GOLDEN HOUR CAME TO COLLECT 🌅", "LIGHT SO GOOD IT'S SUSPICIOUS"],
        "green": ["TOUCHED GRASS, 10/10 WOULD RECOMMEND", "NATURE SAID HI"],
        "blue_moody": ["BLUE BUT MAKE IT DRAMATIC", "MOOD: THIS EXACT SHADE"],
        "clean": ["AESTHETICALLY UNPROBLEMATIC TODAY", "CLEAN GIRL ENERGY ATTEMPT"],
        "everyday": ["THE INTERNET NEEDS TO REST 💀", "NO THOUGHTS, JUST THIS"],
    },
}


def _rnd(options, seed):
    h = int(hashlib.sha1(str(seed).encode("utf-8")).hexdigest()[:8], 16)
    return options[h % len(options)]


def _clean(text, limit=60):
    text = URL_RE.sub("", str(text or ""))
    text = TAG_RE.sub("", text)
    text = "".join(ch for ch in text if ch == " " or ord(ch) >= 32)
    return re.sub(r"\s+", " ", text).strip()[:limit]


SYSTEM = """You write tiny personal Instagram Story captions (thoughts, NEVER headlines) for a
young Indian creator posting THEIR OWN photo. Roman script English or Hinglish only.
Return ONLY JSON: {"captions": [{"text": "...", "intent": "..."}, ...]}
Rules:
- each caption is a small thought that could pop into the user's head looking at this photo
  (e.g. "not me seeing this for the 47th time")
- DIFFERENT intents across captions: funny, relatable, curious, sarcastic, aesthetic
- not synonyms of each other; different angles, not reworded versions of one sentence
- no news language, no "trending", no source talk, no hashtags, no links
- personality voice and emoji count below are strict; never force slang
"""


def generate(ai, persona_key, info, note="", count=3, seed=""):
    """Caption alternatives with different intents. Always returns >=1 candidate."""
    p = PERSONALITIES.get(persona_key, PERSONALITIES["casual_genz"])
    out = []
    if ai is not None and getattr(ai, "ready", False):
        try:
            user = (f"Personality: {persona_key} - {p['caption_voice']}
Photo mood: {info.get('mood')}"
                    f" (busy={info.get('busy')}, bright={info.get('bright')})
"
                    f"Faces in photo: {'yes' if info.get('faces') else 'no'}
"
                    f"User note (context, optional): {note[:200] or 'none'}
"
                    f"Give {count} captions with different intents, max {p['max_words']} words each.")
            raw = json.loads(re.search(r"\{.*}", ai.chat(SYSTEM, user, temperature=0.9, max_tokens=300),
                                       re.S).group(0))
            for c in (raw.get("captions") or []):
                text = _clean(c.get("text"))
                intent = str(c.get("intent") or "").lower()
                if text and intent in INTENTS and len(text.split()) <= p["max_words"] + 2:
                    out.append({"text": text, "intent": intent})
        except Exception:
            out = []
    if not out:
        pool = list(FALLBACK.get(persona_key, FALLBACK["casual_genz"]).get(info.get("mood", "everyday"), []))
        base = note.strip()[:40] or _rnd(pool, seed or info.get("mood"))
        out = [{"text": _clean(base), "intent": p["intents"][0]}]
        for extra in pool:
            if extra != base and len(out) < count:
                out.append({"text": _clean(extra), "intent": _rnd(p["intents"], seed + extra)})
    return out[:count]
