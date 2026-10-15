"""Adaptive photo finishing layered on the existing VibeOS editor.

Requires the project's existing editor.py (Pillow + NumPy). The output is a
BytesIO JPEG, so it can be stored in ContentBuffer or sent to Telegram.
"""
from __future__ import annotations

import io
import math
from typing import Any

import numpy as np
from PIL import Image, ImageEnhance, ImageOps

from .analyzer import analyze_photo, build_edit_plan


def _apply_corrections(image_bytes: bytes, plan: dict[str, Any]) -> bytes:
    img = Image.open(io.BytesIO(image_bytes))
    img.load()
    img = ImageOps.exif_transpose(img).convert("RGB")

    # Exposure in stops -> multiplicative brightness. Keep bounded so the
    # automatic correction cannot radically alter a photo.
    exposure_factor = 2.0 ** float(plan["exposure_stops"])
    img = ImageEnhance.Brightness(img).enhance(exposure_factor)
    contrast_factor = max(0.88, min(1.12, 1.0 + float(plan["contrast_delta"])))
    img = ImageEnhance.Contrast(img).enhance(contrast_factor)
    img = ImageEnhance.Color(img).enhance(float(plan["saturation_multiplier"]))

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=96, subsampling=0, optimize=True)
    return out.getvalue()


def edit_photo_adaptive(
    image_bytes: bytes,
    vibe: str = "natural",
    size: str = "post",
    text: str | None = None,
    text_style: str = "minimal",
    text_position: str = "bottom",
    return_report: bool = False,
):
    """Run existing VibeOS look + image-specific finishing.

    This function deliberately does not invent semantic labels (e.g. "Diwali")
    from pixels. Semantic scene detection should be provided by a vision model
    and stored separately in asset.analysis before planning content.
    """
    try:
        from editor import edit_photo as base_edit_photo
    except ImportError as exc:
        raise RuntimeError("Place creative_studio inside the VibeOS project next to editor.py") from exc

    analysis = analyze_photo(image_bytes)
    plan = build_edit_plan(analysis, vibe=vibe)
    base = base_edit_photo(
        image_bytes, style=vibe, size=size, text=text,
        text_style=text_style, text_position=text_position,
    )
    base_bytes = base.getvalue() if hasattr(base, "getvalue") else bytes(base)
    corrected = _apply_corrections(base_bytes, plan)

    result = io.BytesIO(corrected)
    result.seek(0)
    if return_report:
        return result, {"analysis": analysis, "edit_plan": plan}
    return result
