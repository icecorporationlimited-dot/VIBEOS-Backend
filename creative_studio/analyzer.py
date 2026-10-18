"""Dependency-light photo analysis for VibeOS Creative Studio.

This is deterministic computer-vision analysis, not a semantic vision model.
It estimates image quality/exposure and creates a conservative edit plan. A
vision-capable model can later enrich the plan with scene/subject/style labels.
"""
from __future__ import annotations

import io
from typing import Any

import numpy as np
from PIL import Image, ImageOps, ImageStat

LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)


def _load(image_bytes: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(image_bytes))
    img.load()
    return ImageOps.exif_transpose(img).convert("RGB")


def analyze_photo(image_bytes: bytes) -> dict[str, Any]:
    """Return measurable image properties and conservative edit suggestions."""
    try:
        img = _load(image_bytes)
    except Exception as exc:
        raise ValueError("Input is not a readable image") from exc

    original_width, original_height = img.size
    sample = img.copy()
    sample.thumbnail((720, 720), Image.Resampling.LANCZOS)
    rgb = np.asarray(sample, dtype=np.float32) / 255.0
    luma = rgb @ LUMA
    brightness = float(luma.mean())
    contrast = float(luma.std())
    shadow_clip = float((luma < 0.025).mean())
    highlight_clip = float((luma > 0.975).mean())
    channel_means = rgb.mean(axis=(0, 1))
    saturation = float((rgb.max(axis=2) - rgb.min(axis=2)).mean())

    # Laplacian variance is a rough sharpness proxy, not a universal blur verdict.
    gray = np.asarray(sample.convert("L"), dtype=np.float32)
    if min(gray.shape) >= 3:
        lap = (4 * gray[1:-1, 1:-1] - gray[:-2, 1:-1]
               - gray[2:, 1:-1] - gray[1:-1, :-2] - gray[1:-1, 2:])
        sharpness = float(lap.var())
    else:
        sharpness = 0.0

    suggestions: list[str] = []
    if brightness < 0.38:
        suggestions.append("lift_exposure_and_shadows")
    elif brightness > 0.72:
        suggestions.append("reduce_exposure_and_protect_highlights")
    if contrast < 0.17:
        suggestions.append("add_gentle_contrast")
    if saturation < 0.12:
        suggestions.append("consider_subtle_color_enrichment")
    if highlight_clip > 0.08:
        suggestions.append("protect_clipped_highlights")
    if shadow_clip > 0.10:
        suggestions.append("lift_deep_shadows_carefully")
    if sharpness < 35:
        suggestions.append("avoid_aggressive_sharpening_blur_may_be_present")

    return {
        "width": original_width,
        "height": original_height,
        "aspect_ratio": round(original_width / max(1, original_height), 4),
        "orientation": "landscape" if original_width > original_height else (
            "square" if original_width == original_height else "portrait"),
        "brightness": round(brightness, 4),
        "contrast": round(contrast, 4),
        "saturation_estimate": round(saturation, 4),
        "shadow_clipping_ratio": round(shadow_clip, 4),
        "highlight_clipping_ratio": round(highlight_clip, 4),
        "sharpness_proxy": round(sharpness, 2),
        "mean_rgb": [round(float(v), 4) for v in channel_means],
        "suggestions": suggestions,
        "analysis_kind": "deterministic_pixel_metrics",
        "semantic_scene_detected": False,
    }


def build_edit_plan(analysis: dict[str, Any], vibe: str = "natural") -> dict[str, Any]:
    """Build a bounded correction plan; avoid trying to 'fix' every image hard."""
    vibe = vibe.lower().strip()
    if vibe not in {"natural", "y2k", "moody", "film", "clean", "golden", "mono", "baddie", "soft"}:
        vibe = "natural"

    brightness = float(analysis.get("brightness", 0.5))
    contrast = float(analysis.get("contrast", 0.2))
    saturation = float(analysis.get("saturation_estimate", 0.15))

    # Small, bounded corrections are intentional. The base look remains the
    # existing VibeOS editor preset; these values compensate for image-specific issues.
    exposure = max(-0.16, min(0.16, (0.48 - brightness) * 0.42))
    contrast_delta = max(-0.08, min(0.10, (0.20 - contrast) * 0.35))
    saturation_multiplier = max(0.90, min(1.12, 1.04 if saturation < 0.12 else 1.0))

    if vibe in ("soft", "film"):
        contrast_delta = min(contrast_delta, 0.025)
        saturation_multiplier = min(saturation_multiplier, 1.02)
    elif vibe == "moody":
        contrast_delta = max(contrast_delta, 0.025)
    elif vibe in ("baddie", "y2k"):
        contrast_delta = max(contrast_delta, 0.015)
        saturation_multiplier = min(1.08, max(1.02, saturation_multiplier))

    return {
        "vibe": vibe,
        "exposure_stops": round(exposure, 3),
        "contrast_delta": round(contrast_delta, 3),
        "saturation_multiplier": round(saturation_multiplier, 3),
        "preserve_original": True,
        "text_overlay": False,
        "reason": "bounded corrections based on measured exposure/contrast/color; original is retained",
    }
