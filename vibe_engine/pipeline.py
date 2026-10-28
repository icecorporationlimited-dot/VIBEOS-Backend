"""Staged VIBE pipeline (one prompt never does everything):

  photo understanding -> human reaction generator -> mood/personality match
  -> composition engine -> render -> human-likeness quality check

Input is the user's OWN photo (camera roll energy). No trends, no sources.
"""
from __future__ import annotations

import hashlib
import io

from PIL import Image, ImageOps

from vibe_engine import captions as captions_mod
from vibe_engine import composition, photo_mood, quality, render as render_mod
from vibe_engine.personalities import PERSONALITIES, auto_personality


def _seed(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]


def build_story(photo_bytes: bytes, *, personality: str = "", note: str = "", ai=None,
                recent_layouts=(), variant: str = "") -> dict:
    """One VIBE story from one own photo.

    Returns {"ok", "jpg", "score", "report", "layout", "recent_layouts"}.
    jpg is bytes only when the quality gate passes.
    """
    try:
        photo = ImageOps.exif_transpose(Image.open(io.BytesIO(photo_bytes))).convert("RGB")
    except Exception:
        return {"ok": False, "reason": "unreadable_photo", "jpg": None}

    # 1) photo-first understanding
    info = photo_mood.analyze(photo)
    persona = personality if personality in PERSONALITIES else auto_personality(info, note)

    # 2) human reaction generator (alternatives with different intents)
    seed = _seed(persona, info["mood"], note, variant)
    candidates = captions_mod.generate(ai, persona, info, note=note, seed=seed)

    # 3) pick the caption whose intent fits the persona best, then compose
    wanted = PERSONALITIES[persona]["intents"]
    candidates.sort(key=lambda c: wanted.index(c["intent"]) if c["intent"] in wanted else 9)
    caption = candidates[0]["text"] if candidates else ""

    # no-text mode: very calm scenic photo + soft persona -> photo (+ emoji) only
    if persona == "soft_aesthetic" and info["busy"] < 0.12 and not note.strip():
        caption = ""

    # 4) composition (anti-template via recent_layouts)
    layout = composition.compose(persona, info, caption, recent=recent_layouts, seed=seed)
    layout["faces"] = info["faces"]

    # 5) render
    img = render_mod.render_story(photo, layout)

    # 6) human-likeness quality check
    report = quality.review(caption, layout, info, recent=recent_layouts)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90, optimize=True)
    recent = list(recent_layouts)[-9:] + [layout["signature"]]
    return {"ok": report["ok"], "jpg": out.getvalue() if report["ok"] else None,
            "score": report["score"], "report": report, "layout": layout,
            "photo_info": {k: info[k] for k in ("mood", "busy", "bright", "faces")},
            "caption": caption, "persona": persona, "recent_layouts": recent}
