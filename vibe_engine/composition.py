"""Composition Engine: layered, imperfect, anti-template layouts.

Layers are placed independently (photo treatment / caption / doodles / sticker),
text is never perfectly centred twice in a row, and the anti-template detector
avoids placements used in recent stories.
"""
from __future__ import annotations

import hashlib
import random

# (x, y) anchor per placement, in 0..1 of the text block's top-left
PLACEMENTS = {
    "bottom_left": (0.09, 0.80), "bottom_center": (None, 0.82), "bottom_right": (0.91, 0.80),
    "top_left": (0.09, 0.16), "top_right": (0.91, 0.16), "mid_left": (0.09, 0.52),
    "mid_right": (0.91, 0.52), "corner_tl": (0.06, 0.12), "corner_br": (0.94, 0.84),
}
CENTERED = {"bottom_center"}

# contextual doodles: photo/caption context -> emoji, never random hearts everywhere
DOODLES = {"coffee": "☕", "chai": "☕", "music": "🎧", "song": "🎧", "concert": "🎧",
           "rain": "☔", "barish": "☔", "moon": "🌙", "night": "🌙", "sunset": "🌅",
           "food": "🍽️", "gym": "💪", "match": "🏏", "exam": "📚", "flight": "✈️"}


def _rng(seed):
    return random.Random(int(hashlib.sha1(str(seed).encode("utf-8")).hexdigest()[:12], 16))


def compose(persona_key, info, caption, recent=(), seed=""):
    """Full layout spec for the renderer. recent = list of recent layout signatures."""
    from vibe_engine.personalities import PERSONALITIES
    p = PERSONALITIES.get(persona_key, PERSONALITIES["casual_genz"])
    rnd = _rng(seed or f"{persona_key}{info.get('mood')}{caption}")

    # ---- anti-template detector: avoid placements used in the last stories
    used = {r.rsplit("|", 1)[0] for r in recent if isinstance(r, str)}
    free = [pl for pl in PLACEMENTS if f"{persona_key}:{pl}" not in used] or list(PLACEMENTS)
    # corner bias: imperfect placements beat perfect centring
    pool = [pl for pl in free if pl not in CENTERED] or free
    if rnd.random() > (p["corner_bias"] if pool else 1.0):
        pool = [pl for pl in free if pl in CENTERED] or free
    placement = rnd.choice(pool)

    tilt = round(rnd.uniform(*p["tilt"]), 2)
    size = rnd.randint(*p["caption_size"])
    scrim = p["scrim"] if info.get("busy", 0.2) > 0.18 else max(40, p["scrim"] - 45)
    sticker = rnd.random() < p["sticker_chance"]
    treatment = dict(grain=p["grain"], blur_edge=rnd.random() < 0.6 if p["blur_edge"] else False,
                     zoom=round(rnd.uniform(1.0, 1.12), 3), vignette=p["blur_edge"])
    doodles = []
    if caption:
        for key, emoji in DOODLES.items():
            if key in caption.lower() and len(doodles) < 2:
                doodles.append(emoji)
    if not doodles and persona_key == "chaotic_meme" and rnd.random() < 0.5:
        doodles.append("✨")
    # no-text mode: very calm scenic photo + soft persona -> photo + emoji only
    no_text = caption is None or (not caption and bool(doodles))
    signature = f"{persona_key}:{placement}|{size // 8}|{'notext' if no_text else 'text'}|{len(doodles)}"
    return dict(persona=persona_key, mood=info.get("mood"), placement=placement, tilt=tilt,
                size=size, scrim=scrim, sticker=sticker, treatment=treatment,
                doodles=doodles, caption=caption or "", no_text=no_text,
                signature=signature, calm_cell=info.get("faces") and None or None)
