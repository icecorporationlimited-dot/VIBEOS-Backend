"""Creator personality profiles for the VIBE engine.

Every personality is a full design language: caption voice, typography,
imperfection rules and photo treatment. The composition engine reads these
instead of using one fixed template.
"""
from __future__ import annotations

# font: key in the vibe engine font map (bold/medium/light/serif=handwritten vibe)
PERSONALITIES = {
    "casual_genz": dict(
        caption_voice=("natural gen-z reaction, lowercase, light slang only when it fits, 1-2 emojis. "
                       "short thought like 'bro why is this literally everywhere 😭'"),
        intents=("relatable", "funny", "curious"),
        font="medium", lowercase=True, emoji_dose=(1, 2),
        caption_size=(44, 58), max_words=12,
        tilt=(-3.0, 3.0), corner_bias=0.75,
        grain=0.07, blur_edge=True, scrim=110,
        sticker_chance=0.25,
    ),
    "soft_aesthetic": dict(
        caption_voice=("quiet poetic thought, minimal, calm, no slang, at most one soft emoji. "
                       "like 'some days just feel different.'"),
        intents=("aesthetic", "relatable", "curious"),
        font="serif", lowercase=True, emoji_dose=(0, 1),
        caption_size=(36, 48), max_words=10,
        tilt=(-1.5, 1.5), corner_bias=0.5,
        grain=0.04, blur_edge=False, scrim=70,
        sticker_chance=0.0,
    ),
    "chaotic_meme": dict(
        caption_voice=("loud chaotic meme reaction, ALL CAPS ok, 2-3 emojis, internet-brain energy. "
                       "like 'THE INTERNET NEEDS TO REST 💀'"),
        intents=("funny", "sarcastic", "relatable"),
        font="bold", lowercase=False, emoji_dose=(2, 3),
        caption_size=(52, 72), max_words=10,
        tilt=(-5.0, 5.0), corner_bias=0.6,
        grain=0.09, blur_edge=True, scrim=130,
        sticker_chance=0.55,
    ),
}
DEFAULT = "casual_genz"

# mood -> default personality (photo-first: the photo decides the vibe)
MOOD_PERSONA = {
    "night": "soft_aesthetic", "blue_moody": "soft_aesthetic", "clean": "soft_aesthetic",
    "sunset": "soft_aesthetic", "golden": "casual_genz", "green": "casual_genz",
    "everyday": "casual_genz",
}


def auto_personality(info: dict, note: str = "") -> str:
    """Photo decides the personality; a chaotic note can override."""
    if any(w in note.lower() for w in ("💀", "lol", "meme", "bruh")):
        return "chaotic_meme"
    return MOOD_PERSONA.get(info.get("mood", "everyday"), DEFAULT)
