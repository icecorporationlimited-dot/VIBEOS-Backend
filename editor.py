"""
VibeOS Pro Editor
Photo finishing that fixes the photo first, then applies the look:

  1. subject-aware crop (face detection when OpenCV is installed, otherwise a
     built-in saliency/skin map) so heads are never cut in 4:5 / 9:16 crops
  2. auto fix: white balance, adaptive exposure, shadow lift, highlight
     recovery, chroma denoise for noisy night shots
  3. style grade: S-curve, vibrance (skin-safe), split-tone, fade, clarity
  4. finish: bloom, vignette, grain, resolution-aware sharpening, text overlay

Only Pillow + NumPy are required. OpenCV (opencv-python-headless) is optional
and is picked up automatically for better face detection.

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

try:  # optional: better face detection
    import cv2  # type: ignore
except Exception:  # pragma: no cover - depends on host
    cv2 = None

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
# saturation: 1 = unchanged (>1 is applied as skin-safe vibrance)
# warmth: +warm / -cool | fade: lifts blacks (matte look)
# shadows/highlights: RGB split-tone | clarity: local contrast (0-0.5)
# target: auto-exposure aim (median brightness, default 0.46)
# bloom: glow on highlights | vignette | grain | sharpen: unsharp percent
# ---------------------------------------------------------------------------
STYLES = {
    "natural": dict(
        exposure=1.02, contrast=0.14, saturation=1.10, warmth=0.015,
        fade=0.0, shadows=(0, 0, 0), highlights=(0, 0, 0),
        clarity=0.10, bloom=0.08, vignette=0.10, grain=0.008, sharpen=70,
    ),
    "baddie": dict(
        exposure=1.02, contrast=0.30, saturation=1.18, warmth=0.040,
        fade=0.01, shadows=(-0.015, 0.0, 0.03), highlights=(0.035, 0.012, -0.02),
        clarity=0.18, bloom=0.26, vignette=0.22, grain=0.014, sharpen=90,
    ),
    "soft": dict(
        exposure=1.05, contrast=0.06, saturation=0.94, warmth=0.030,
        fade=0.08, shadows=(0.02, 0.0, 0.02), highlights=(0.03, 0.01, 0.015),
        clarity=0.0, bloom=0.40, vignette=0.06, grain=0.008, sharpen=40,
    ),
    "moody": dict(
        target=0.36,   # keep the dark mood: auto-exposure aims lower
        exposure=0.93, contrast=0.34, saturation=0.84, warmth=-0.005,
        fade=0.035, shadows=(-0.03, 0.012, 0.045), highlights=(0.045, 0.015, -0.035),
        clarity=0.16, bloom=0.14, vignette=0.38, grain=0.028, sharpen=80,
    ),
    # warm golden-hour glow
    "golden": dict(
        exposure=1.04, contrast=0.18, saturation=1.16, warmth=0.075,
        fade=0.02, shadows=(0.02, 0.005, -0.015), highlights=(0.05, 0.022, -0.04),
        clarity=0.10, bloom=0.34, vignette=0.14, grain=0.010, sharpen=70,
    ),
    # bright, airy, polished (skincare / minimal feeds)
    "clean": dict(
        exposure=1.07, contrast=0.12, saturation=1.0, warmth=-0.006,
        fade=0.0, shadows=(0.012, 0.012, 0.02), highlights=(0, 0, 0),
        clarity=0.14, bloom=0.18, vignette=0.0, grain=0.0, sharpen=85,
    ),
    # analog film: lifted blacks, green-teal shadows, warm highlights
    "film": dict(
        exposure=1.0, contrast=0.20, saturation=0.92, warmth=0.022,
        fade=0.06, shadows=(-0.012, 0.018, 0.012), highlights=(0.04, 0.02, -0.02),
        clarity=0.04, bloom=0.16, vignette=0.20, grain=0.032, sharpen=50,
    ),
    # black & white: pure grayscale, strong contrast, light grain
    "mono": dict(
        exposure=1.02, contrast=0.40, saturation=0.0, warmth=0.0,
        fade=0.0, shadows=(0, 0, 0), highlights=(0, 0, 0),
        clarity=0.18, bloom=0.06, vignette=0.22, grain=0.022, sharpen=80,
    ),
}

LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _load(image_bytes, max_side=2600):
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img).convert("RGB")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return img


def _blur_map(m, radius):
    """Gaussian-blur a 0..1 float map using PIL (fast, no SciPy needed)."""
    im = Image.fromarray((np.clip(m, 0, 1) * 255).astype(np.uint8))
    return np.asarray(im.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32) / 255.0


def _skin_mask(a):
    """Rough skin-tone mask (0/1). Used to protect faces from over-saturation,
    harsh clarity and yellow highlight tint, and as a people cue for cropping."""
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx = a.max(-1)
    d = mx - a.min(-1)
    sat = d / (mx + 1e-6)
    h6 = (g - b) / (d + 1e-6)          # hue in sixths; 0..0.85 is red-orange
    skin = (r >= g) & (g >= b) & (h6 <= 0.85) & (sat > 0.12) & (sat < 0.75) & (mx > 0.25)
    return skin.astype(np.float32)


def _skin_soft(a):
    h, w, _ = a.shape
    return _blur_map(_skin_mask(a), max(1.0, max(w, h) * 0.004))


# ---------------------------------------------------------------------------
# Subject-aware crop
# ---------------------------------------------------------------------------
_FACE_CASCADE = None


def _face_focus(img):
    """(cx, cy, w, h) of the face group, all 0..1, or None. Needs OpenCV."""
    global _FACE_CASCADE
    if cv2 is None:
        return None
    try:
        if _FACE_CASCADE is None:
            path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            casc = cv2.CascadeClassifier(path)
            _FACE_CASCADE = casc if not casc.empty() else False
        if _FACE_CASCADE is False:
            return None
        small = img.copy()
        small.thumbnail((640, 640))
        gray = cv2.equalizeHist(cv2.cvtColor(np.asarray(small), cv2.COLOR_RGB2GRAY))
        sh, sw = gray.shape
        min_side = max(24, int(min(sh, sw) * 0.08))
        faces = _FACE_CASCADE.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=6, minSize=(min_side, min_side))
        if len(faces) == 0:
            return None
        biggest = max(int(f[2]) * int(f[3]) for f in faces)
        keep = [f for f in faces if int(f[2]) * int(f[3]) >= 0.3 * biggest]
        x0 = min(int(f[0]) for f in keep)
        y0 = min(int(f[1]) for f in keep)
        x1 = max(int(f[0]) + int(f[2]) for f in keep)
        y1 = max(int(f[1]) + int(f[3]) for f in keep)
        return ((x0 + x1) / 2 / sw, (y0 + y1) / 2 / sh, (x1 - x0) / sw, (y1 - y0) / sh)
    except Exception:
        return None


def _saliency_focus(img):
    """Pure-NumPy fallback: where is the subject? Edges + skin tones, with a
    mild centre prior so the crop never runs off to a corner."""
    small = img.copy()
    small.thumbnail((160, 160))
    a = np.asarray(small, dtype=np.float32) / 255.0
    lum = a @ LUMA
    gy, gx = np.gradient(lum)
    edge = _blur_map(np.hypot(gx, gy) / (np.hypot(gx, gy).max() + 1e-6), 3)
    skin = _blur_map(_skin_mask(a), 3)
    sal = edge * 0.6 + skin * 0.9
    h, w = sal.shape
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    xn, yn = (x + 0.5) / w, (y + 0.5) / h
    prior = np.exp(-(((xn - 0.5) / 0.45) ** 2 + ((yn - 0.5) / 0.45) ** 2))
    wts = (sal * (0.4 + 0.6 * prior)) ** 2
    total = float(wts.sum())
    if total <= 1e-6:
        return (0.5, 0.5)
    return (float((wts * xn).sum() / total), float((wts * yn).sum() / total))


def _find_focus(img):
    """-> (fx, fy, fw, fh, source) with source in face | saliency."""
    face = _face_focus(img)
    if face:
        return (*face, "face")
    fx, fy = _saliency_focus(img)
    return (fx, fy, 0.0, 0.0, "saliency")


def _crop_box(w, h, target, focus):
    """Float crop box (l, t, r, b) with the target aspect, centred on the subject."""
    tw, th = target
    tr, r = tw / th, w / h
    if abs(r - tr) < 0.004:
        return (0.0, 0.0, float(w), float(h))
    fx, fy, fw, fh, src = focus
    if r > tr:                                   # too wide: trim the sides
        nw = h * tr
        x0 = min(max(fx * w - nw / 2, 0.0), w - nw)
        return (x0, 0.0, x0 + nw, float(h))
    nh = w / tr                                  # too tall: trim top/bottom
    anchor = 0.36 if src == "face" else 0.42     # subject sits a bit above centre
    y0 = fy * h - anchor * nh
    if src == "face":                            # never cut the head or chin
        top, bottom = (fy - fh / 2) * h, (fy + fh / 2) * h
        y0 = min(y0, top - 0.05 * nh)
        y0 = max(y0, bottom + 0.05 * nh - nh)
    y0 = min(max(y0, 0.0), h - nh)
    return (0.0, y0, float(w), y0 + nh)


def _smart_crop(img, size, report=None):
    w, h = img.size
    tw, th = size
    if abs(w / h - tw / th) < 0.004:
        box, src = (0.0, 0.0, float(w), float(h)), "none"
    else:
        focus = _find_focus(img)
        box, src = _crop_box(w, h, size, focus), focus[4]
    if report is not None:
        report["crop"] = src
    return img.resize(size, Image.Resampling.LANCZOS, box=box)


# ---------------------------------------------------------------------------
# Auto fix (runs before the style): the photo is repaired, then styled
# ---------------------------------------------------------------------------
def _sample(a, step=4):
    return a[::step, ::step]


def _estimate_noise(img):
    """Rough luma noise sigma (0..1) from flat areas. Higher = noisier."""
    g = np.asarray(img.convert("L"), dtype=np.float32)[::2, ::2] / 255.0
    if min(g.shape) < 8:
        return 0.0
    hp = g[1:-1, 1:-1] - (g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:]) / 4
    gy, gx = np.gradient(g)
    grad = np.hypot(gx, gy)[1:-1, 1:-1]
    flat = grad < np.percentile(grad, 40)
    if int(flat.sum()) < 100:
        return 0.0
    return float(np.median(np.abs(hp[flat])) / 0.6745 * 0.894)


def _denoise(img):
    """Chroma blur + light median on luma, only when the photo is noisy."""
    sigma = _estimate_noise(img)
    if sigma <= 0.010:
        return img, sigma
    k = min(1.0, (sigma - 0.010) / 0.03)
    y, cb, cr = img.convert("YCbCr").split()
    rad = 1.0 + 3.0 * k
    cb, cr = cb.filter(ImageFilter.GaussianBlur(rad)), cr.filter(ImageFilter.GaussianBlur(rad))
    y = Image.blend(y, y.filter(ImageFilter.MedianFilter(3)), 0.7 * k)
    return Image.merge("YCbCr", (y, cb, cr)).convert("RGB"), sigma


def _auto_white_balance(a, max_gain=0.10, strength=0.7):
    """Gentle gray-world on neutral-ish mid-tones. Max +-7% so golden light
    and intentional colour casts survive."""
    s = _sample(a)
    lum = s @ LUMA
    mx, mn = s.max(-1), s.min(-1)
    sat = (mx - mn) / (mx + 1e-6)
    m = (lum > 0.15) & (lum < 0.85) & (sat < 0.35)
    if float(m.mean()) < 0.05:
        return a, (1.0, 1.0, 1.0)
    means = s[m].mean(0)
    gains = 1 + (means.mean() / (means + 1e-6) - 1) * strength
    gains = np.clip(gains, 1 - max_gain, 1 + max_gain)
    gains = gains / float(gains @ LUMA)            # keep brightness unchanged
    return np.clip(a * gains, 0, 1), tuple(round(float(g), 3) for g in gains)


def _auto_exposure(a, target=0.46, strength=0.7, lo=-0.6, hi=1.3):
    """Move the median brightness toward `target` (0.46 = mid-grey) by a
    fraction `strength` of the needed correction, in stops of linear light,
    with a soft highlight shoulder so brightening does not clip."""
    med = float(np.median(_sample(a) @ LUMA))
    stops = hi if med <= 0.01 else float(2.2 * np.log2(target / med) * strength)
    stops = max(lo, min(hi, stops))
    if abs(stops) < 0.03:
        return a, 0.0
    lin = np.power(a, 2.2) * (2.0 ** stops)
    t = np.clip(lin - 0.8, 0, None) / 0.2
    lin = np.where(lin > 0.8, 0.8 + 0.2 * t / (1 + t), lin)   # shoulder, asymptote 1.0
    return np.clip(np.power(lin, 1 / 2.2), 0, 1), round(stops, 3)


def _recover_tones(a):
    """Lift crushed shadows and pull back blown highlights, using a blurred
    luminance mask so there are no halos."""
    s = _sample(a) @ LUMA
    p10 = float(np.percentile(s, 10))
    clip_frac = float((s > 0.96).mean())
    shadow_amt = min(1.0, max(0.0, (0.22 - p10) / 0.22)) * 0.45
    high_amt = min(0.30, clip_frac * 3.0)
    if shadow_amt < 0.02 and high_amt < 0.02:
        return a, (0.0, 0.0)
    h, w, _ = a.shape
    lb = _blur_map(a @ LUMA, max(2.0, max(w, h) * 0.01))
    gain = 1 + shadow_amt * 2.0 * (1 - lb) ** 3
    gain = gain * (1 - high_amt * np.clip((lb - 0.7) / 0.3, 0, 1) ** 2)
    return np.clip(a * gain[..., None], 0, 1), (round(shadow_amt, 3), round(high_amt, 3))


def _auto_levels(a, low=0.5, high=99.5):
    s = _sample(a, 3)
    lo = np.percentile(s, low)
    hi = np.percentile(s, high)
    if hi - lo < 0.2:  # flat image, don't over-stretch
        return a
    # gentle: only blend 60% of the stretch so it never looks crunchy
    stretched = np.clip((a - lo) / (hi - lo), 0, 1)
    return a * 0.4 + stretched * 0.6


# ---------------------------------------------------------------------------
# Style grade (float32 arrays, 0..1)
# ---------------------------------------------------------------------------
def _saturate(a, s):
    """s < 1 desaturates evenly; s > 1 is vibrance: boosts dull colours more
    than vivid ones and mostly leaves skin tones alone."""
    if abs(s - 1.0) < 1e-3:
        return a
    lum = (a * LUMA).sum(-1, keepdims=True)
    if s < 1:
        return np.clip(lum + (a - lum) * s, 0, 1)
    mx = a.max(-1, keepdims=True)
    sat = (mx - a.min(-1, keepdims=True)) / (mx + 1e-6)
    skin = _skin_mask(a)[..., None]
    gain = 1 + (s - 1) * 1.35 * (1 - sat) ** 1.2 * (1 - 0.65 * skin)
    return np.clip(lum + (a - lum) * gain, 0, 1)


def _grade(a, p, skin):
    a = np.clip(a * p["exposure"], 0, 1)

    # S-curve contrast (smoothstep blend)
    c = p["contrast"]
    s = a * a * (3 - 2 * a)
    a = a * (1 - c) + s * c

    a = _saturate(a, p["saturation"])

    # warmth
    w = p["warmth"]
    a[..., 0] += w
    a[..., 2] -= w

    # split toning: tint shadows and highlights separately.
    # Highlight tint is halved on skin so faces never turn jaundiced.
    lum = (np.clip(a, 0, 1) * LUMA).sum(-1, keepdims=True)
    sh_w = (1 - lum) ** 2
    hi_w = lum ** 2 * (1 - 0.5 * skin[..., None])
    a += sh_w * np.array(p["shadows"], dtype=np.float32)
    a += hi_w * np.array(p["highlights"], dtype=np.float32)

    # matte fade (lifted blacks)
    f = p["fade"]
    a = a * (1 - f) + f
    return np.clip(a, 0, 1)


def _clarity(a, amount, skin):
    """Local contrast on mid-tones; reduced on skin so pores are not exaggerated."""
    if amount <= 0:
        return a
    h, w, _ = a.shape
    lum = a @ LUMA
    detail = lum - _blur_map(lum, max(2.0, max(w, h) * 0.010))
    weight = np.clip(1 - np.abs(lum - 0.5) * 1.5, 0.15, 1.0) * (1 - 0.6 * skin)
    return np.clip(a + (detail * weight * amount * 1.6)[..., None], 0, 1)


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


def _apply_style(img, style, report=None):
    p = STYLES.get(style, STYLES["natural"])
    rng = np.random.default_rng()

    img, noise = _denoise(img)
    a = np.asarray(img, dtype=np.float32) / 255.0

    a, wb = _auto_white_balance(a)
    a, stops = _auto_exposure(a, target=p.get("target", 0.46))
    a, rec = _recover_tones(a)
    a = _auto_levels(a)
    if report is not None:
        report.update(noise=round(noise, 4), wb_gain=wb, exposure_stops=stops,
                      shadow_lift=rec[0], highlight_recovery=rec[1])

    skin = _skin_soft(a)
    a = _grade(a, p, skin)
    a = _clarity(a, p.get("clarity", 0.0), skin)
    a = _bloom(a, p["bloom"])
    a = _vignette(a, p["vignette"])
    a = _grain(a, p["grain"], rng)

    out = Image.fromarray((np.clip(a, 0, 1) * 255).astype(np.uint8))
    radius = max(1.0, 0.0012 * max(out.size))      # same look at any resolution
    return out.filter(ImageFilter.UnsharpMask(radius=radius, percent=p["sharpen"], threshold=2))

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
    return_report=False,
):
    """Return a finished JPEG as BytesIO (or (BytesIO, report) if return_report).

    style:         natural | baddie | soft | moody | golden | clean | film
    size:          post (4:5) | story (9:16) | square | original
    text:          optional overlay line (emoji are stripped)
    text_style:    minimal | bold | serif
    text_position: bottom | center | top
    report:        what the auto-fix did (crop source, exposure, noise ...)
    """
    report = {"style": style if style in STYLES else "natural"}
    img = _load(image_bytes)

    if size in SIZES:
        img = _smart_crop(img, SIZES[size], report)
    else:
        report["crop"] = "none"

    img = _apply_style(img, style, report)

    if text:
        img = _add_text(
            img, text, text_style, text_position, is_story=(size == "story")
        )

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95, subsampling=0, optimize=True)
    out.seek(0)
    return (out, report) if return_report else out
