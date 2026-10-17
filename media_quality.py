"""
media_quality.py — Phase 1 quality checks (pure functions, no DB/network).

analyze_image(bytes) -> dict with:
    width, height, orientation, aspect
    sharpness, brightness, contrast, clipped_shadows, clipped_highlights
    dhash (16 hex chars), quality_score (0-100)
    issues (hard problems -> asset must not be published)
    notes  (soft hints -> fixable, e.g. crop needed)
"""
import io

import numpy as np
from PIL import Image, ImageOps

ANALYSIS_SIDE = 512          # all measurements are done at this size
MIN_SHORT_SIDE = 720         # soft ranking hint, not an automatic rejection
BLUR_THRESHOLD = 40.0        # soft-focus hint, not an automatic rejection
DARK_THRESHOLD = 55.0        # mean luma (0-255); underexposure hint
BRIGHT_THRESHOLD = 205.0     # overexposure hint
LOW_CONTRAST_STD = 28.0
DUPLICATE_DISTANCE = 6       # dHash hamming distance <= this -> duplicate
SIMILAR_DISTANCE = 12        # <= this -> too similar to shortlist twice

# Reject only files that cannot be decoded. Other quality problems are ranking hints.
HARD_ISSUES = {"unreadable"}

_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)


def load_oriented(image_bytes) -> Image.Image:
    """Open an image honouring EXIF rotation, as RGB."""
    img = Image.open(io.BytesIO(image_bytes))
    img.load()
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def _laplacian_var(gray: np.ndarray) -> float:
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0
    c = gray[1:-1, 1:-1]
    lap = (
        4 * c
        - gray[:-2, 1:-1]
        - gray[2:, 1:-1]
        - gray[1:-1, :-2]
        - gray[1:-1, 2:]
    )
    return float(lap.var())


def _tile_sharpness(gray: np.ndarray, grid=4) -> float:
    """90th-percentile tile sharpness.

    A portrait with a blurry background is still a sharp photo, so we judge
    by the sharpest regions, not the global average.
    """
    h, w = gray.shape
    th, tw = max(h // grid, 3), max(w // grid, 3)
    values = []
    for y in range(0, h - th + 1, th):
        for x in range(0, w - tw + 1, tw):
            values.append(_laplacian_var(gray[y:y + th, x:x + tw]))
    return float(np.percentile(values, 90)) if values else 0.0


def dhash(img: Image.Image, size=8) -> str:
    """64-bit difference hash as 16 hex chars."""
    g = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    a = np.asarray(g, dtype=np.int16)
    bits = (a[:, 1:] > a[:, :-1]).flatten()
    value = 0
    for b in bits:
        value = (value << 1) | int(b)
    return f"{value:0{size * size // 4}x}"


def hamming(hash_a: str, hash_b: str) -> int:
    return bin(int(hash_a, 16) ^ int(hash_b, 16)).count("1")


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def analyze_image(image_bytes) -> dict:
    try:
        img = load_oriented(image_bytes)
    except Exception:
        return {
            "width": 0, "height": 0, "orientation": "unknown", "aspect": 0,
            "sharpness": 0, "brightness": 0, "contrast": 0,
            "clipped_shadows": 0, "clipped_highlights": 0,
            "dhash": None, "quality_score": 0,
            "issues": ["unreadable"], "notes": [],
        }

    width, height = img.size
    aspect = round(width / height, 3)
    if abs(aspect - 1) < 0.05:
        orientation = "square"
    elif aspect < 1:
        orientation = "portrait"
    else:
        orientation = "landscape"

    small = img.copy()
    small.thumbnail((ANALYSIS_SIDE, ANALYSIS_SIDE), Image.LANCZOS)
    arr = np.asarray(small, dtype=np.float32)
    gray = arr @ _LUMA

    sharpness = _tile_sharpness(gray)
    brightness = float(gray.mean())
    contrast = float(gray.std())
    clipped_shadows = float((gray < 8).mean())
    clipped_highlights = float((gray > 247).mean())

    issues, notes = [], []
    # Technical quality signals lower the ranking, but should not discard an
    # otherwise usable image before VIBEOS can shortlist or enhance it.
    if min(width, height) < MIN_SHORT_SIDE:
        notes.append("too_small")
    if sharpness < BLUR_THRESHOLD:
        notes.append("blurry")
    if brightness < DARK_THRESHOLD:
        notes.append("too_dark")
    if brightness > BRIGHT_THRESHOLD:
        notes.append("too_bright")
    if contrast < LOW_CONTRAST_STD:
        notes.append("low_contrast")
    if clipped_highlights > 0.15:
        notes.append("blown_highlights")
    if aspect < 0.5625 or aspect > 1.91:
        notes.append("needs_crop")
    if orientation == "landscape":
        notes.append("landscape_will_be_cropped")

    # ---- score: sharpness 40 | exposure 25 | contrast 15 | resolution 20
    s_sharp = _clamp(sharpness / 250.0) * 40
    s_expo = _clamp(1 - abs(brightness - 128) / 100.0) * 25
    s_contrast = _clamp(contrast / 60.0) * 15
    s_res = _clamp(min(width, height) / 1080.0) * 20
    # Keep soft technical warnings meaningful without letting several fixable
    # issues collapse every image to zero. The component scores already account
    # for sharpness, exposure and resolution, so these are modest extra penalties.
    technical_notes = {"too_small", "blurry", "too_dark", "too_bright", "low_contrast", "blown_highlights"}
    technical_penalty = 5 * sum(note in technical_notes for note in notes)
    presentation_penalty = 5 * sum(note not in technical_notes for note in notes)
    penalty = technical_penalty + presentation_penalty
    quality = int(round(_clamp((s_sharp + s_expo + s_contrast + s_res - penalty) / 100.0) * 100))

    return {
        "width": width,
        "height": height,
        "orientation": orientation,
        "aspect": aspect,
        "sharpness": round(sharpness, 1),
        "brightness": round(brightness, 1),
        "contrast": round(contrast, 1),
        "clipped_shadows": round(clipped_shadows, 3),
        "clipped_highlights": round(clipped_highlights, 3),
        "dhash": dhash(img),
        "quality_score": quality,
        "issues": issues,
        "notes": notes,
    }


def has_hard_issue(analysis: dict) -> bool:
    return bool(set(analysis.get("issues", [])) & HARD_ISSUES)
