"""
VibeOS Pro Editor
Celebrity-style photo finishing: smart crop, color grading, glow, vignette,
film grain, sharpening and clean text overlays.

Usage:
    from editor import edit_photo
    out = edit_photo(image_bytes, "baddie", size="post",
                     text="main character energy", text_style="bold")
    # `out` is a BytesIO (JPEG), ready for reply_photo(photo=out)
"""
import io
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

FONT_DIR = Path(__file__).resolve().parent / "fonts"

# Output sizes (Instagram native)
SIZES = {
    "post": (1080, 1350),    # 4:5 feed portrait
    "story": (1080, 1920),   # 9:16 story / reel
    "square": (1080, 1080),
}

# ---------------------------------------------------------------------------
# Grading presets
# exposure: brightness multiplier | contrast: 0-0.5 S-curve strength
# saturation: 1 = unchanged | warmth: +warm / -cool
# fade: lifts blacks (matte look) | shadows/highlights: RGB split-tone
# bloom: glow on highlights | vignette | grain | sharpen: unsharp percent
# ---------------------------------------------------------------------------
STYLES = {
    "natural": dict(
        exposure=1.03, contrast=0.14, saturation=1.08, warmth=0.015,
        fade=0.0, shadows=(0, 0, 0), highlights=(0, 0, 0),
        bloom=0.10, vignette=0.12, grain=0.010, sharpen=70,
    ),
    "baddie": dict(
        exposure=1.02, contrast=0.30, saturation=1.14, warmth=0.040,
        fade=0.01, shadows=(-0.015, 0.0, 0.03), highlights=(0.035, 0.012, -0.02),
        bloom=0.28, vignette=0.22, grain=0.016, sharpen=90,
    ),
    "soft": dict(
        exposure=1.05, contrast=0.06, saturation=0.92, warmth=0.030,
        fade=0.08, shadows=(0.02, 0.0, 0.02), highlights=(0.03, 0.01, 0.015),
        bloom=0.42, vignette=0.06, grain=0.008, sharpen=40,
    ),
    "moody": dict(
        exposure=0.93, contrast=0.34, saturation=0.82, warmth=-0.005,
        fade=0.035, shadows=(-0.03, 0.012, 0.045), highlights=(0.045, 0.015, -0.035),
        bloom=0.14, vignette=0.38, grain=0.030, sharpen=80,
    ),
}

LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)


# ---------------------------------------------------------------------------
# Loading + cropping
# ---------------------------------------------------------------------------
def _load(image_bytes, max_side=2600):
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img).convert("RGB")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return img


def _smart_crop(img, size):
    # centering y=0.38 keeps heads/faces from being cut in portrait crops
    return ImageOps.fit(
        img, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.38)
    )


# ---------------------------------------------------------------------------
# Color pipeline (float32 arrays, 0..1)
# ---------------------------------------------------------------------------
def _auto_levels(a, low=0.5, high=99.5):
    lo = np.percentile(a, low)
    hi = np.percentile(a, high)
    if hi - lo < 0.2:  # flat image, don't over-stretch
        return a
    # gentle: only blend 60% of the stretch so it never looks crunchy
    stretched = np.clip((a - lo) / (hi - lo), 0, 1)
    return a * 0.4 + stretched * 0.6


def _grade(a, p):
    a = np.clip(a * p["exposure"], 0, 1)

    # S-curve contrast (smoothstep blend)
    c = p["contrast"]
    s = a * a * (3 - 2 * a)
    a = a * (1 - c) + s * c

    # saturation
    lum = (a * LUMA).sum(-1, keepdims=True)
    a = np.clip(lum + (a - lum) * p["saturation"], 0, 1)

    # warmth
    w = p["warmth"]
    a[..., 0] += w
    a[..., 2] -= w

    # split toning: tint shadows and highlights separately
    lum = (np.clip(a, 0, 1) * LUMA).sum(-1, keepdims=True)
    sh_w = (1 - lum) ** 2
    hi_w = lum ** 2
    a += sh_w * np.array(p["shadows"], dtype=np.float32)
    a += hi_w * np.array(p["highlights"], dtype=np.float32)

    # matte fade (lifted blacks)
    f = p["fade"]
    a = a * (1 - f) + f
    return np.clip(a, 0, 1)


def _bloom(a, strength):
    """Soft glow on highlights (screen blend) for that polished look."""
    if strength <= 0:
        return a
    h, w, _ = a.shape
    lum = (a * LUMA).sum(-1, keepdims=True)
    mask = np.clip((lum - 0.55) / 0.45, 0, 1)
    bright = Image.fromarray((a * mask * 255).astype(np.uint8))
    radius = max(w, h) * 0.018
    blurred = np.asarray(
        bright.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32
    ) / 255.0
    return 1 - (1 - a) * (1 - blurred * strength)


def _vignette(a, strength):
    if strength <= 0:
        return a
    h, w, _ = a.shape
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    d = np.sqrt(((x / w - 0.5) / 0.5) ** 2 + ((y / h - 0.5) / 0.5) ** 2)
    mask = np.clip((d - 0.55) / 0.95, 0, 1) ** 2
    return a * (1 - strength * mask[..., None])


def _grain(a, amount, rng):
    if amount <= 0:
        return a
    h, w, _ = a.shape
    noise = rng.normal(0, amount, (h, w, 1)).astype(np.float32)
    lum = (a * LUMA).sum(-1, keepdims=True)
    weight = 1 - np.abs(lum - 0.5) * 1.2  # strongest in midtones
    return np.clip(a + noise * weight, 0, 1)


def _apply_style(img, style):
    p = STYLES.get(style, STYLES["natural"])
    rng = np.random.default_rng()
    a = np.asarray(img, dtype=np.float32) / 255.0

    a = _auto_levels(a)
    a = _grade(a, p)
    a = _bloom(a, p["bloom"])
    a = _vignette(a, p["vignette"])
    a = _grain(a, p["grain"], rng)

    out = Image.fromarray((np.clip(a, 0, 1) * 255).astype(np.uint8))
    return out.filter(
        ImageFilter.UnsharpMask(radius=1.6, percent=p["sharpen"], threshold=2)
    )


# ---------------------------------------------------------------------------
# Text overlay
# ---------------------------------------------------------------------------
def _font(kind, size):
    files = {
        "light": ["Poppins-Light.ttf"],
        "medium": ["Poppins-Medium.ttf"],
        "bold": ["Poppins-Bold.ttf"],
        "serif": ["Lora-Italic-Variable.ttf"],
    }[kind]
    candidates = [FONT_DIR / f for f in files] + [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf"),
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _clean_text(text):
    # Remove emoji / unsupported symbols (fonts render them as boxes)
    text = "".join(ch for ch in str(text) if ord(ch) < 0x2070)
    return re.sub(r"\s+", " ", text).strip()


# style -> (font kind, size as fraction of width, uppercase, tracking em)
TEXT_STYLES = {
    "minimal": ("light", 0.040, False, 0.14),
    "bold": ("bold", 0.078, True, 0.02),
    "serif": ("serif", 0.062, False, 0.0),
}


def _text_width(text, font, tracking_px):
    if tracking_px == 0:
        return font.getlength(text)
    return sum(font.getlength(ch) + tracking_px for ch in text) - tracking_px


def _wrap(text, font, tracking_px, max_width):
    lines, current = [], ""
    for word in text.split(" "):
        trial = f"{current} {word}".strip()
        if _text_width(trial, font, tracking_px) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _draw_line(draw, x, y, text, font, tracking_px, fill):
    if tracking_px == 0:
        draw.text((x, y), text, font=font, fill=fill)
        return
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += font.getlength(ch) + tracking_px


def _scrim(img, position):
    """Soft dark gradient behind text so it stays readable on any photo."""
    w, h = img.size
    band = int(h * 0.38)
    ramp = np.linspace(0, 1, band, dtype=np.float32) ** 1.8
    alpha = (ramp * 0.50 * 255).astype(np.uint8)
    if position == "top":
        alpha = alpha[::-1]
    mask = np.tile(alpha[:, None], (1, w))
    layer = Image.new("RGBA", (w, band), (0, 0, 0, 0))
    layer.putalpha(Image.fromarray(mask))
    out = img.convert("RGBA")
    y0 = 0 if position == "top" else h - band
    out.alpha_composite(layer, (0, y0))
    return out


def _add_text(img, text, style="minimal", position="bottom", is_story=False):
    text = _clean_text(text)
    if not text:
        return img

    kind, size_frac, upper, track_em = TEXT_STYLES.get(
        style, TEXT_STYLES["minimal"]
    )
    if upper:
        text = text.upper()

    w, h = img.size
    font = _font(kind, int(w * size_frac))
    tracking_px = font.size * track_em
    max_width = w * 0.80
    lines = _wrap(text, font, tracking_px, max_width)[:4]

    line_h = int(font.size * 1.28)
    block_h = line_h * len(lines)

    # keep clear of Instagram UI (story top bar / reply box)
    top_safe = int(h * (0.14 if is_story else 0.07))
    bottom_safe = int(h * (0.20 if is_story else 0.08))

    if position == "top":
        y = top_safe
    elif position == "center":
        y = (h - block_h) // 2
    else:
        y = h - bottom_safe - block_h

    base = _scrim(img, position) if position in ("top", "bottom") else img.convert("RGBA")

    shadow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    fg = Image.new("RGBA", base.size, (0, 0, 0, 0))
    fdraw = ImageDraw.Draw(fg)

    for i, line in enumerate(lines):
        lw = _text_width(line, font, tracking_px)
        x = (w - lw) / 2
        ly = y + i * line_h
        off = max(2, int(w * 0.003))
        _draw_line(sdraw, x + off, ly + off, line, font, tracking_px, (0, 0, 0, 170))
        _draw_line(fdraw, x, ly, line, font, tracking_px, (255, 255, 255, 255))

    shadow = shadow.filter(ImageFilter.GaussianBlur(max(2, w * 0.005)))
    base.alpha_composite(shadow)
    base.alpha_composite(fg)
    return base.convert("RGB")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def edit_photo(
    image_bytes,
    style="natural",
    size="post",
    text=None,
    text_style="minimal",
    text_position="bottom",
):
    """Return a finished JPEG as BytesIO.

    style:         natural | baddie | soft | moody
    size:          post (4:5) | story (9:16) | square | original
    text:          optional overlay line (emoji are stripped)
    text_style:    minimal | bold | serif
    text_position: bottom | center | top
    """
    img = _load(image_bytes)

    if size in SIZES:
        img = _smart_crop(img, SIZES[size])

    img = _apply_style(img, style)

    if text:
        img = _add_text(
            img, text, text_style, text_position, is_story=(size == "story")
        )

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95, subsampling=0, optimize=True)
    out.seek(0)
    return out
