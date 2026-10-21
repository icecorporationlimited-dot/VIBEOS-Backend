"""
VibeOS Pro Editor v2 - six adaptive looks, face-safe framing, low RAM.

Looks (editor.STYLES):  y2k | moody | film | clean | golden | mono
"natural" = AUTO: the look is chosen per photo from the tags sent by the
tagger server (mono, film, glow, night, streetwear ...) and from the pixels.

Pipeline (all heavy work happens at the OUTPUT size, never at 12 MP):
  1. fit      face-safe framing: crop only when no face is cut, else the photo
              is kept whole on a blurred, darkened copy of itself
  2. measure  exposure, range, cast, noise, skin share  -> per-photo parameters
  3. fix      white balance, exposure, shadow / highlight recovery, denoise
  4. grade    tone curve, vibrance (skin-safe), split-tone, look specials
  5. finish   clarity, bloom, vignette, grain, sharpening
  6. text     ONLY when `text` is given; placed away from faces

Memory: ~120-180 MB peak for a 12 MP phone photo -> 1080x1350 (measured, see
tests/test_editor_memory.py); Pillow + NumPy only. OpenCV is optional.

    from editor import edit_photo
    out = edit_photo(jpeg_bytes, "natural", size="post",
                     tags=["night", "party"], faces=[[0.31, 0.12, 0.18, 0.14]])
"""
import gc
import hashlib
import io
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from media_names import resolve_style

try:  # optional: local face detection when the tagger sends no face boxes
    import cv2  # type: ignore
except Exception:  # pragma: no cover
    cv2 = None

FONT_DIR = Path(__file__).resolve().parent / "fonts"

SIZES = {
    "post": (1080, 1350),    # 4:5 feed portrait
    "story": (1080, 1920),   # 9:16 story / reel
    "square": (1080, 1080),
}
ORIGINAL_MAX = 1800          # size="original": longest side cap (RAM guard)

# ---------------------------------------------------------------------------
# The six looks.  Every value is a BASE; _adapt() bends it per photo.
#   target    auto-exposure aim (median luma)      strength  how far to go (0-1)
#   exposure  extra multiplier                      contrast  S-curve (0-0.5)
#   saturation  <1 mutes, >1 skin-safe vibrance     warmth    +warm / -cool
#   fade      lifted blacks                         shadows/highlights  RGB tint
#   clarity   local contrast                        bloom (+bloom_tint) glow
#   vignette, grain (+grain_px clump size), sharpen (unsharp %)
#   wb/wb_max white-balance strength / max gain     levels    auto-levels blend
#   flash     direct-flash falloff                  separation  subject pop
#   ab        chromatic offset (px @1080)           chroma_noise  digicam noise
#   cast_cap  max R/B ratio (no orange cast)        mix       mono channel mixer
# ---------------------------------------------------------------------------
STYLES = {
    "y2k": dict(          # Y2K / Digicam Flash
        target=0.38, strength=0.55, exposure=1.0, contrast=0.36, saturation=1.10,
        warmth=-0.004, fade=0.03, shadows=(-0.012, 0.020, 0.026),
        highlights=(0.030, 0.020, -0.020), clarity=0.14, bloom=0.16,
        bloom_tint=(1.0, 0.95, 0.85), vignette=0.20, grain=0.030, grain_px=1.2,
        sharpen=120, wb=0.35, wb_max=0.08, levels=0.30, flash=1.0, ab=1,
        chroma_noise=0.012),
    "moody": dict(        # Cinematic Moody
        target=0.38, strength=0.60, exposure=0.96, contrast=0.27, saturation=0.90,
        warmth=0.0, fade=0.020, shadows=(-0.025, 0.006, 0.040),
        highlights=(0.045, 0.016, -0.030), clarity=0.14, bloom=0.10,
        bloom_tint=(1.0, 0.92, 0.80), vignette=0.26, grain=0.010, grain_px=1.2,
        sharpen=80, wb=0.60, wb_max=0.08, levels=0.35, separation=0.6),
    "film": dict(         # Soft Film / Warm Vintage
        target=0.48, strength=0.60, exposure=1.03, contrast=0.12, saturation=0.86,
        warmth=0.028, fade=0.075, shadows=(0.012, 0.012, 0.004),
        highlights=(0.035, 0.020, 0.0), clarity=0.0, bloom=0.22,
        bloom_tint=(1.0, 0.72, 0.50), vignette=0.10, grain=0.020, grain_px=1.5,
        sharpen=35, wb=0.25, wb_max=0.06, levels=0.35),
    "clean": dict(        # Clean Editorial
        target=0.49, strength=0.80, exposure=1.03, contrast=0.11, saturation=1.02,
        warmth=0.0, fade=0.0, shadows=(0, 0, 0), highlights=(0, 0, 0),
        clarity=0.16, bloom=0.03, bloom_tint=(1, 1, 1), vignette=0.0, grain=0.0,
        grain_px=1.0, sharpen=100, wb=1.0, wb_max=0.16, levels=0.60),
    "golden": dict(       # Golden Hour Glow
        target=0.47, strength=0.60, exposure=1.03, contrast=0.16, saturation=1.10,
        warmth=0.035, fade=0.02, shadows=(0.015, 0.006, -0.008),
        highlights=(0.050, 0.024, -0.035), clarity=0.06, bloom=0.34,
        bloom_tint=(1.0, 0.80, 0.55), vignette=0.12, grain=0.008, grain_px=1.2,
        sharpen=60, wb=0.20, wb_max=0.05, levels=0.40, cast_cap=1.30),
    "mono": dict(         # Monochrome / B&W
        target=0.46, strength=0.70, exposure=1.02, contrast=0.38, saturation=0.0,
        warmth=0.0, fade=0.0, shadows=(0, 0, 0), highlights=(0, 0, 0),
        clarity=0.20, bloom=0.05, bloom_tint=(1, 1, 1), vignette=0.20, grain=0.022,
        grain_px=1.3, sharpen=85, wb=0.0, wb_max=0.0, levels=1.0, mix=True),
}
STYLE_NAMES = ("natural",) + tuple(STYLES)        # must equal media_names.STYLES

# scene -> (look, tweaks). Used when the tagger sends scene tags instead of a look.
SCENES = {
    "night": ("y2k", {}), "party": ("y2k", {}), "club": ("y2k", {}),
    "nightlife": ("y2k", {}), "nightout": ("y2k", {}),
    "sunset": ("golden", {}), "sunrise": ("golden", {}),
    "goldenhour": ("golden", {}), "sunlight": ("golden", {}),
    "streetwear": ("moody", dict(sat_cap=0.92, separation_mul=1.3)),
    "street": ("moody", dict(sat_cap=0.95)), "urban": ("moody", {}),
    "architecture": ("moody", dict(sky=0.7)), "dramatic": ("moody", {}),
    "travel": ("film", dict(sky=1.0, warm_highlights=1.3, sat_floor=0.95)),
    "cafe": ("film", {}), "coffee": ("film", {}), "candid": ("film", {}),
    "dump": ("film", {}), "vintage": ("film", {}),
    "fashion": ("clean", dict(wb=1.0, sat_cap=1.03, sharpen_add=15, no_grain=True)),
    "outfit": ("clean", dict(wb=1.0, sat_cap=1.03, sharpen_add=15, no_grain=True)),
    "product": ("clean", dict(wb=1.0, no_grain=True)),
    "brand": ("clean", dict(wb=1.0, no_grain=True)),
    "professional": ("clean", dict(no_grain=True)),
    "studio": ("clean", {}), "minimal": ("clean", {}),
    "selfie": ("clean", dict(contrast_mul=0.8, clarity_mul=0.4, sat_cap=1.06,
                             grain_mul=0.4, skin_care=True)),
    "casual": ("clean", dict(contrast_mul=0.8, clarity_mul=0.4, sat_cap=1.06,
                             grain_mul=0.4, skin_care=True)),
    "portrait": ("clean", dict(clarity_mul=0.6, skin_care=True)),
}

LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _norm(tag):
    return re.sub(r"[^a-z0-9]", "", str(tag or "").lower())


def _flat_tags(tags):
    """tags may be a list, a str, or the tagger's dict -> set of normalised words."""
    out = set()

    def add(v):
        if v is None:
            return
        if isinstance(v, dict):
            for key, val in v.items():
                if key in ("style", "edit", "scene", "tags", "labels", "vibe", "look"):
                    add(val)
        elif isinstance(v, (list, tuple, set)):
            for x in v:
                add(x)
        else:
            for part in re.split(r"[,/|;]+", str(v)):
                n = _norm(part)
                if n:
                    out.add(n)
    add(tags)
    return out


def _up(m, size):
    """Upsample a small 0..1 float map (h, w) to size=(W, H) as float32."""
    im = Image.fromarray((np.clip(m, 0, 1) * 255).astype(np.uint8))
    return np.asarray(im.resize(size, Image.Resampling.BILINEAR), dtype=np.float32) / 255.0


def _blur_small(m, radius):
    im = Image.fromarray((np.clip(m, 0, 1) * 255).astype(np.uint8))
    return np.asarray(im.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32) / 255.0


def _skin_mask(a):
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx = a.max(-1)
    d = mx - a.min(-1)
    sat = d / (mx + 1e-6)
    h6 = (g - b) / (d + 1e-6)
    skin = (r >= g) & (g >= b) & (h6 <= 0.85) & (sat > 0.12) & (sat < 0.75) & (mx > 0.25)
    return skin.astype(np.float32)


def _skin_full(a):
    """Soft skin map at full working size, computed on a 1/4 copy (cheap)."""
    h, w, _ = a.shape
    small = _skin_mask(a[::4, ::4])
    return _up(_blur_small(small, 1.5), (w, h))


# ---------------------------------------------------------------------------
# 1. Loading + face-safe framing
# ---------------------------------------------------------------------------
def _load(image_bytes, size):
    long_side = ORIGINAL_MAX if size not in SIZES else max(SIZES[size])
    img = Image.open(io.BytesIO(image_bytes))
    try:                                   # JPEG: decode at 1/2..1/8 scale = far less RAM
        img.draft("RGB", (int(long_side * 1.3), int(long_side * 1.3)))
    except Exception:
        pass
    img = ImageOps.exif_transpose(img).convert("RGB")
    cap = int(long_side * 1.8) if size in SIZES else ORIGINAL_MAX
    if max(img.size) > cap:
        img.thumbnail((cap, cap), Image.Resampling.LANCZOS, reducing_gap=2.0)
    return img


def _clean_faces(faces):
    """-> list of (x, y, w, h) top-left, all 0..1. Ignores junk."""
    out = []
    for f in faces or []:
        try:
            x, y, w, h = (float(v) for v in f[:4])
        except Exception:
            continue
        if w <= 0 or h <= 0:
            continue
        x0, y0 = min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)
        x1, y1 = min(x + w, 1.0), min(y + h, 1.0)
        if x1 - x0 > 0.01 and y1 - y0 > 0.01:
            out.append((x0, y0, x1 - x0, y1 - y0))
    return out


_FACE_CASCADE = None


def _detect_faces_cv(img):
    """Optional OpenCV Haar fallback -> normalised (x, y, w, h) list."""
    global _FACE_CASCADE
    if cv2 is None:
        return []
    try:
        if _FACE_CASCADE is None:
            casc = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            _FACE_CASCADE = casc if not casc.empty() else False
        if _FACE_CASCADE is False:
            return []
        small = img.copy()
        small.thumbnail((640, 640))
        gray = cv2.equalizeHist(cv2.cvtColor(np.asarray(small), cv2.COLOR_RGB2GRAY))
        sh, sw = gray.shape
        m = max(24, int(min(sh, sw) * 0.06))
        found = _FACE_CASCADE.detectMultiScale(gray, 1.1, 5, minSize=(m, m))
        return [(x / sw, y / sh, fw / sw, fh / sh) for (x, y, fw, fh) in found]
    except Exception:
        return []


def _quick_look(img):
    """(skin share, saliency point) from a 160 px thumbnail - for crop decisions."""
    small = img.copy()
    small.thumbnail((160, 160))
    a = np.asarray(small, dtype=np.float32) / 255.0
    skin = _skin_mask(a)
    lum = a @ LUMA
    gy, gx = np.gradient(lum)
    edge = np.hypot(gx, gy)
    edge = _blur_small(edge / (edge.max() + 1e-6), 3)
    sal = edge * 0.6 + _blur_small(skin, 3) * 0.9
    h, w = sal.shape
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    xn, yn = (x + 0.5) / w, (y + 0.5) / h
    prior = np.exp(-(((xn - 0.5) / 0.45) ** 2 + ((yn - 0.5) / 0.45) ** 2))
    wts = (sal * (0.4 + 0.6 * prior)) ** 2
    tot = float(wts.sum())
    pt = (0.5, 0.5) if tot <= 1e-6 else (float((wts * xn).sum() / tot),
                                         float((wts * yn).sum() / tot))
    return float(skin.mean()), pt


SAFE_TRIM = 0.10        # without face info and with people likely: trim at most 10 %
SCENERY_TRIM = 0.35     # no people: a bigger crop is fine
FACE_TRIM = 0.50        # faces known: crop up to 50 % as long as every face stays inside


def _group(faces):
    x0 = min(f[0] for f in faces)
    y0 = min(f[1] for f in faces)
    x1 = max(f[0] + f[2] for f in faces)
    y1 = max(f[1] + f[3] for f in faces)
    return x0, y0, x1, y1


def _crop_box(w, h, tr, faces, pt):
    """Crop box (l, t, r, b) in px with target ratio `tr`; None if a face would be cut."""
    r = w / h
    if faces:
        gx0, gy0, gx1, gy1 = _group(faces)
        cx, cy = (gx0 + gx1) / 2, (gy0 + gy1) / 2
    else:
        gx0 = gx1 = cx = pt[0]
        gy0 = gy1 = cy = pt[1]
    if r > tr:                                         # too wide: trim the sides
        nw = h * tr
        x0 = min(max(cx * w - nw / 2, 0.0), w - nw)
        if faces:
            pad = 0.03 * nw
            if gx0 * w < x0 + pad or gx1 * w > x0 + nw - pad:
                return None
        return (x0, 0.0, x0 + nw, float(h))
    nh = w / tr                                        # too tall: trim top/bottom
    y0 = cy * h - (0.36 if faces else 0.42) * nh
    if faces:                                          # headroom + never cut the chin
        top, bottom = gy0 * h, gy1 * h
        y0 = min(y0, top - 0.06 * nh)
        y0 = max(y0, bottom + 0.05 * nh - nh)
        y0 = min(max(y0, 0.0), h - nh)
        if top < y0 + 0.02 * nh or bottom > y0 + nh - 0.02 * nh:
            return None
    else:
        y0 = min(max(y0, 0.0), h - nh)
    return (0.0, y0, float(w), y0 + nh)


def _blur_fill(img, size, anchor_y=0.5):
    """Whole photo, centred, over a blurred + darkened copy of itself. -> (img, (px, py, fw, fh))."""
    tw, th = size
    bg = ImageOps.fit(img, (max(8, tw // 8), max(8, th // 8)), Image.Resampling.BILINEAR)
    bg = bg.filter(ImageFilter.GaussianBlur(3)).resize(size, Image.Resampling.BICUBIC)
    bg = ImageEnhance.Brightness(bg).enhance(0.62)
    bg = ImageEnhance.Color(bg).enhance(0.85)
    fg = ImageOps.contain(img, size, Image.Resampling.LANCZOS)
    px = (tw - fg.width) // 2
    py = int((th - fg.height) * anchor_y)
    bg.paste(fg, (px, py))
    return bg, (px, py, fg.width, fg.height)


def _fit(img, size, faces, report):
    """-> (img at `size`, faces in output coordinates). Never cuts a known face."""
    w, h = img.size
    tw, th = size
    tr, r = tw / th, w / h
    faces = _clean_faces(faces)
    if abs(r / tr - 1) < 0.01:
        report["crop"] = "none"
        return img.resize(size, Image.Resampling.LANCZOS), faces
    trim = 1 - (tr / r if r > tr else r / tr)
    skin, pt = _quick_look(img)
    people_risk = skin > 0.035
    if faces:
        report["faces"] = len(faces)
        limit = FACE_TRIM
    else:
        limit = SAFE_TRIM if people_risk else SCENERY_TRIM
    box = _crop_box(w, h, tr, faces, pt) if trim <= limit else None
    if box is not None:
        l, t, rr, b = box
        out = img.resize(size, Image.Resampling.LANCZOS, box=box)
        moved = [((x * w - l) / (rr - l), (y * h - t) / (b - t),
                  fw * w / (rr - l), fh * h / (b - t)) for x, y, fw, fh in faces]
        report["crop"] = "face" if faces else ("saliency" if not people_risk else "safe")
        return out, moved
    out, (px, py, fw, fh) = _blur_fill(img, size, 0.46 if th > tw else 0.5)
    moved = [((px + x * fw) / tw, (py + y * fh) / th, f_w * fw / tw, f_h * fh / th)
             for x, y, f_w, f_h in faces]
    report["crop"] = "pad"
    return out, moved


# ---------------------------------------------------------------------------
# 2. Measure + adapt
# ---------------------------------------------------------------------------
def _estimate_noise(img):
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


def _denoise(img, sigma):
    if sigma <= 0.010:
        return img
    k = min(1.0, (sigma - 0.010) / 0.03)
    y, cb, cr = img.convert("YCbCr").split()
    rad = 1.0 + 3.0 * k
    cb, cr = cb.filter(ImageFilter.GaussianBlur(rad)), cr.filter(ImageFilter.GaussianBlur(rad))
    y = Image.blend(y, y.filter(ImageFilter.MedianFilter(3)), 0.7 * k)
    return Image.merge("YCbCr", (y, cb, cr)).convert("RGB")


def _measure(a, noise):
    s = a[::4, ::4]
    lum = s @ LUMA
    mx, mn = s.max(-1), s.min(-1)
    sat = (mx - mn) / (mx + 1e-6)
    neutral = (lum > 0.15) & (lum < 0.85) & (sat < 0.35)
    if float(neutral.mean()) > 0.05:
        m = s[neutral].mean(0)
        cast = float(m[0] / (m[2] + 1e-6))
    else:
        cast = 1.0
    h, w = lum.shape
    cy0, cy1, cx0, cx1 = h // 4, 3 * h // 4, w // 4, 3 * w // 4
    centre = float(lum[cy0:cy1, cx0:cx1].mean())
    edge = float((lum.sum() - lum[cy0:cy1, cx0:cx1].sum())
                 / max(1, lum.size - lum[cy0:cy1, cx0:cx1].size))
    top = s[: max(1, h // 3)]
    blue_top = (top[..., 2] > top[..., 0] + 0.03) & (top[..., 2] > top[..., 1])
    return dict(
        med=float(np.median(lum)), p5=float(np.percentile(lum, 5)),
        p95=float(np.percentile(lum, 95)), hi_clip=float((lum > 0.97).mean()),
        sat=float(sat.mean()), cast=cast, noise=noise,
        skin=float(_skin_mask(s).mean()), flashy=(centre - edge) > 0.15,
        sky=float(blue_top.mean()),
    )


def choose_style(stats, tags=None):
    """Pick one of STYLES for a photo. Tag priority: explicit look > scene > pixels."""
    words = _flat_tags(tags)
    for wd in words:                                     # 1. a look named by the tagger
        r = resolve_style(wd, default="")
        if r and r != "natural":
            return r, None
    for wd in SCENES:                                    # 2. a scene (priority = table order)
        if wd in words:
            if wd == "travel" and ({"sunset", "sunrise", "goldenhour"} & words):
                return "golden", "travel"
            return SCENES[wd][0], wd
    med = stats["med"]                                   # 3. the pixels
    if med < 0.26:
        return ("y2k" if stats["flashy"] else "moody"), None
    if stats["cast"] > 1.20 and med > 0.38 and stats["sat"] > 0.18:
        return "golden", None
    return "clean", None


def pick_batch_style(styles):
    """Photo dump: one shared look for the batch (the most common pick)."""
    styles = [s for s in styles if s in STYLES]
    return max(set(styles), key=styles.count) if styles else "clean"


def _adapt(p, st, tweaks, rng, adaptive_plan=None):
    """Bend the base look to THIS photo, then add a tiny per-photo jitter so a
    batch shares colours but no two frames come out identical."""
    p = dict(p)
    spread = st["p95"] - st["p5"]
    if spread > 0.78:
        p["contrast"] *= 0.55
    elif spread < 0.50:
        p["contrast"] = min(0.5, p["contrast"] * 1.25)
    if st["sat"] > 0.36 and p["saturation"] > 1:
        p["saturation"] = 1 + (p["saturation"] - 1) * 0.4
    elif st["sat"] < 0.10 and 0 < p["saturation"] < 1.3:
        p["saturation"] += 0.04
    if st["med"] < 0.22:
        p["vignette"] *= 0.6
    if st["hi_clip"] > 0.05:
        p["bloom"] *= 0.5
    p["grain"] *= max(0.25, 1 - st["noise"] * 30)        # noisy source: less added grain
    if p.get("flash"):                                   # flash strength follows the light
        night = st["med"] < 0.30
        p["flash"] = (1.0 if night else 0.5) * (0.6 if st["flashy"] else 1.0)
    if tweaks:
        p["contrast"] *= tweaks.get("contrast_mul", 1.0)
        p["clarity"] *= tweaks.get("clarity_mul", 1.0)
        p["grain"] *= tweaks.get("grain_mul", 1.0)
        if "sat_cap" in tweaks and p["saturation"] > tweaks["sat_cap"]:
            p["saturation"] = tweaks["sat_cap"]
        if "sat_floor" in tweaks and 0 < p["saturation"] < tweaks["sat_floor"]:
            p["saturation"] = tweaks["sat_floor"]
        if "wb" in tweaks:
            p["wb"], p["wb_max"] = tweaks["wb"], max(p["wb_max"], 0.14)
        p["sharpen"] += tweaks.get("sharpen_add", 0)
        p["separation"] = p.get("separation", 0) * tweaks.get("separation_mul", 1.0)
        if tweaks.get("no_grain"):
            p["grain"] = 0.0
        p["sky"] = tweaks.get("sky", 0.0)
        p["skin_care"] = tweaks.get("skin_care", False)
        if "warm_highlights" in tweaks:
            p["highlights"] = tuple(v * tweaks["warm_highlights"] for v in p["highlights"])

    # Optional values from the separate style analyzer. They are deliberately
    # blended into the renderer's own per-image adaptation rather than applied
    # as a second full correction pass.
    if isinstance(adaptive_plan, dict):
        try:
            ev = max(-0.20, min(0.20, float(adaptive_plan.get("exposure", 0.0))))
            p["exposure"] *= 2.0 ** ev
        except (TypeError, ValueError):
            pass
        try:
            contrast_hint = max(-30.0, min(35.0, float(adaptive_plan.get("contrast", 0.0))))
            p["contrast"] = max(0.0, min(0.5, p["contrast"] + contrast_hint / 1000.0))
        except (TypeError, ValueError):
            pass
        try:
            saturation_hint = max(-100.0, min(25.0, float(adaptive_plan.get("saturation", 0.0))))
            p["saturation"] = max(0.0, min(1.25, p["saturation"] * (1.0 + saturation_hint / 125.0)))
        except (TypeError, ValueError):
            pass
        try:
            warmth_hint = max(-25.0, min(25.0, float(adaptive_plan.get("warmth", 0.0))))
            p["warmth"] += warmth_hint / 1800.0
        except (TypeError, ValueError):
            pass
        try:
            grain_hint = max(0.0, min(25.0, float(adaptive_plan.get("grain", 0.0))))
            p["grain"] = max(0.0, min(0.05, p["grain"] * 0.75 + grain_hint / 1000.0))
        except (TypeError, ValueError):
            pass

    j = lambda amt: float(rng.uniform(-amt, amt))        # noqa: E731
    p["contrast"] = max(0.0, p["contrast"] * (1 + j(0.06)))
    p["warmth"] += j(0.005)
    p["exposure"] *= 1 + j(0.015)
    p["fade"] = max(0.0, p["fade"] + j(0.006))
    return p


# ---------------------------------------------------------------------------
# 3. Fix (in place on float32 H x W x 3 arrays)
# ---------------------------------------------------------------------------
def _auto_white_balance(a, strength, max_gain):
    if strength <= 0:
        return (1.0, 1.0, 1.0)
    s = a[::4, ::4]
    lum = s @ LUMA
    sat = (s.max(-1) - s.min(-1)) / (s.max(-1) + 1e-6)
    m = (lum > 0.15) & (lum < 0.85) & (sat < 0.35)
    if float(m.mean()) < 0.05:
        return (1.0, 1.0, 1.0)
    means = s[m].mean(0)
    gains = 1 + (means.mean() / (means + 1e-6) - 1) * strength
    gains = np.clip(gains, 1 - max_gain, 1 + max_gain)
    gains = (gains / float(gains @ LUMA)).astype(np.float32)
    a *= gains
    np.clip(a, 0, 1, out=a)
    return tuple(round(float(g), 3) for g in gains)


def _auto_exposure(a, target, strength, lo=-0.6, hi=1.3):
    med = float(np.median(a[::4, ::4] @ LUMA))
    stops = hi if med <= 0.01 else float(2.2 * np.log2(target / med) * strength)
    stops = max(lo, min(hi, stops))
    if abs(stops) < 0.03:
        return 0.0
    np.power(a, 2.2, out=a)
    a *= 2.0 ** stops
    over = a > 0.8                                       # soft highlight shoulder
    if over.any():
        t = (a[over] - 0.8) / 0.2
        a[over] = 0.8 + 0.2 * t / (1 + t)
    del over
    np.clip(a, 0, 1, out=a)
    np.power(a, 1 / 2.2, out=a)
    return round(stops, 3)


def _recover_tones(a, lum_small):
    p10 = float(np.percentile(lum_small, 10))
    clip_frac = float((lum_small > 0.96).mean())
    shadow_amt = min(1.0, max(0.0, (0.22 - p10) / 0.22)) * 0.45
    high_amt = min(0.30, clip_frac * 3.0)
    if shadow_amt < 0.02 and high_amt < 0.02:
        return (0.0, 0.0)
    h, w, _ = a.shape
    lb = _up(_blur_small(a[::4, ::4] @ LUMA, 3), (w, h))   # smooth mask: no halos
    gain = 1 + shadow_amt * 2.0 * (1 - lb) ** 3
    gain *= 1 - high_amt * np.clip((lb - 0.7) / 0.3, 0, 1) ** 2
    a *= gain[..., None]
    np.clip(a, 0, 1, out=a)
    return (round(shadow_amt, 3), round(high_amt, 3))


def _levels(a, blend, lo_pct=0.5, hi_pct=99.5, out_lo=0.0, out_hi=1.0):
    if blend <= 0:
        return
    s = a[::6, ::6]
    lo, hi = np.percentile(s, lo_pct), np.percentile(s, hi_pct)
    if hi - lo < 0.2:
        return
    t = (a - lo) / (hi - lo)
    np.clip(t, 0, 1, out=t)
    t *= out_hi - out_lo
    t += out_lo
    a *= 1 - blend
    t *= blend
    a += t


# ---------------------------------------------------------------------------
# 4. Grade
# ---------------------------------------------------------------------------
def _contrast(a, c):
    if c <= 0:
        return
    s = a * a * (3 - 2 * a)
    a *= 1 - c
    s *= c
    a += s


def _saturate(a, s, skin):
    if abs(s - 1.0) < 1e-3:
        return
    lum = (a * LUMA).sum(-1, keepdims=True)
    if s < 1:
        a -= lum
        a *= s
        a += lum
    else:
        mx = a.max(-1, keepdims=True)
        sat = (mx - a.min(-1, keepdims=True)) / (mx + 1e-6)
        gain = 1 + (s - 1) * 1.35 * (1 - sat) ** 1.2 * (1 - 0.65 * skin[..., None])
        a -= lum
        a *= gain
        a += lum
    np.clip(a, 0, 1, out=a)


def _split_tone(a, shadows, highlights, skin):
    if not any(shadows) and not any(highlights):
        return
    lum = (np.clip(a, 0, 1) * LUMA).sum(-1, keepdims=True)
    sh_w = (1 - lum) ** 2
    hi_w = lum ** 2 * (1 - 0.5 * skin[..., None])        # no jaundiced faces
    a += sh_w * np.array(shadows, dtype=np.float32)
    a += hi_w * np.array(highlights, dtype=np.float32)


def _mono(a, st):
    """Channel mixer chosen from content (skin -> red-ish filter, sky -> darker sky)."""
    if st["skin"] > 0.06:
        wts = (0.45, 0.45, 0.10)
    elif st["sky"] > 0.15:
        wts = (0.42, 0.50, 0.08)
    else:
        wts = (0.34, 0.54, 0.12)
    g = a @ np.array(wts, dtype=np.float32)
    a[...] = g[..., None]


def _subject_mask(size, faces, pt):
    """Soft 0..1 'where the subject is' map from faces (face + body) or a saliency point."""
    tw, th = size
    gw, gh = 48, max(8, int(48 * th / tw))
    yy, xx = np.mgrid[0:gh, 0:gw].astype(np.float32)
    xx, yy = (xx + 0.5) / gw, (yy + 0.5) / gh
    m = np.zeros((gh, gw), np.float32)
    if faces:
        for x, y, w, h in faces:
            cx, cy = x + w / 2, y + h / 2 + 0.9 * h        # body sits below the face
            rx, ry = max(1.6 * w, 0.22), max(3.2 * h, 0.30)
            m = np.maximum(m, np.exp(-(((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2)))
    else:
        rx, ry = 0.38, 0.45
        m = np.exp(-(((xx - pt[0]) / rx) ** 2 + ((yy - pt[1]) / ry) ** 2))
    return _up(m, (tw, th))


def _flash(a, mask, k):
    """Direct-flash look: subject lifted, background falls off into shadow."""
    gain = 1 + 0.20 * k * mask - 0.36 * k * (1 - mask)
    a *= gain[..., None]
    np.clip(a, 0, 1, out=a)


def _separate(a, mask, k):
    """Cinematic subject separation: darker, slightly muted background."""
    if k <= 0:
        return
    bg = 1 - mask
    a *= (1 - 0.16 * k * bg)[..., None]
    lum = (a * LUMA).sum(-1, keepdims=True)
    a -= lum
    a *= (1 - 0.12 * k * bg)[..., None]
    a += lum
    a *= (1 + 0.04 * k * mask)[..., None]
    np.clip(a, 0, 1, out=a)


def _sky_balance(a, k):
    """Pull blown sky down a little and give it body (travel / architecture)."""
    if k <= 0:
        return
    h, w, _ = a.shape
    s = a[::4, ::4]
    lum = s @ LUMA
    sky = ((s[..., 2] > s[..., 0] + 0.02) & (s[..., 2] > s[..., 1] - 0.02) & (lum > 0.5)
           ).astype(np.float32)
    rows = np.linspace(1.0, 0.0, sky.shape[0], dtype=np.float32)[:, None]   # top-weighted
    m = _up(_blur_small(sky * rows, 2), (w, h))
    a *= (1 - 0.10 * k * m)[..., None]
    a[..., 2] *= 1 + 0.03 * k * m
    np.clip(a, 0, 1, out=a)


def _cast_guard(a, cap):
    """Golden light without an orange cast: limit the global R/B ratio."""
    s = a[::6, ::6]
    lum = s @ LUMA
    keep = (lum > 0.15) & (lum < 0.9)
    if float(keep.mean()) < 0.05:
        return 1.0
    m = s[keep].mean(0)
    ratio = float(m[0] / (m[2] + 1e-6))
    if ratio <= cap:
        return round(ratio, 3)
    f = (cap / ratio) ** 0.5
    a[..., 0] *= f
    a[..., 2] /= f
    np.clip(a, 0, 1, out=a)
    return round(ratio, 3)


# ---------------------------------------------------------------------------
# 5. Finish
# ---------------------------------------------------------------------------
def _clarity(a, amount, skin):
    if amount <= 0:
        return
    h, w, _ = a.shape
    lum = a @ LUMA
    half = lum[::2, ::2]
    blur = _up(_blur_small(half, max(2.0, max(w, h) * 0.005)), (w, h))
    detail = lum - blur
    weight = np.clip(1 - np.abs(lum - 0.5) * 1.5, 0.15, 1.0) * (1 - 0.6 * skin)
    detail *= weight * amount * 1.6
    a += detail[..., None]
    np.clip(a, 0, 1, out=a)


def _bloom(a, strength, tint):
    if strength <= 0:
        return
    h, w, _ = a.shape
    s = a[::4, ::4]
    lum = (s * LUMA).sum(-1, keepdims=True)
    mask = np.clip((lum - 0.55) / 0.45, 0, 1)
    bright = Image.fromarray((s * mask * 255).astype(np.uint8))
    blurred = bright.filter(ImageFilter.GaussianBlur(max(2.0, max(w, h) / 4 * 0.02)))
    b = np.asarray(blurred.resize((w, h), Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
    b *= strength * np.array(tint, dtype=np.float32)
    np.subtract(1.0, b, out=b)
    np.subtract(1.0, a, out=a)
    a *= b
    np.subtract(1.0, a, out=a)


def _vignette(a, strength):
    if strength <= 0:
        return
    h, w, _ = a.shape
    xs = (((np.arange(w, dtype=np.float32) + 0.5) / w) - 0.5) / 0.5
    ys = (((np.arange(h, dtype=np.float32) + 0.5) / h) - 0.5) / 0.5
    d = np.sqrt(ys[:, None] ** 2 + xs[None, :] ** 2)
    m = np.clip((d - 0.55) / 0.95, 0, 1) ** 2
    a *= (1 - strength * m)[..., None]


def _grain(a, amount, px, rng, chroma=0.0):
    if amount <= 0 and chroma <= 0:
        return
    h, w, _ = a.shape
    lum = a @ LUMA
    weight = 1 - np.abs(lum - 0.5) * 1.2                 # strongest in the mid-tones
    if amount > 0:
        if px > 1.05:                                    # clumped, film-like grain
            gh, gw = int(h / px) + 1, int(w / px) + 1
            raw = rng.standard_normal((gh, gw), dtype=np.float32)
            im = Image.fromarray(np.clip(raw * 40 + 128, 0, 255).astype(np.uint8))
            n = (np.asarray(im.resize((w, h), Image.Resampling.BICUBIC), dtype=np.float32)
                 - 128) / 40 * 1.5
        else:
            n = rng.standard_normal((h, w), dtype=np.float32)
        n *= weight * amount
        a += n[..., None]
    if chroma > 0:                                       # digicam colour noise
        for c in range(3):
            a[..., c] += rng.standard_normal((h, w), dtype=np.float32) * (chroma * weight)
    np.clip(a, 0, 1, out=a)


def _chromatic(a, px):
    s = max(1, int(round(px * a.shape[1] / 1080)))
    red = a[:, :-s, 0].copy()
    a[:, s:, 0] = red
    blue = a[:, s:, 2].copy()
    a[:, :-s, 2] = blue


def _style_pass(img, style, tweaks, faces, pt, rng, report, adaptive_plan=None):
    """The whole fix -> grade -> finish chain on one image. Returns a PIL image."""
    base = STYLES[style]
    noise = _estimate_noise(img)
    img = _denoise(img, noise)
    a = np.array(img, dtype=np.float32)
    a /= 255.0
    del img
    gc.collect()

    st = _measure(a, noise)
    p = _adapt(base, st, tweaks, rng, adaptive_plan=adaptive_plan)
    report.update(style=style, noise=round(noise, 4), med=round(st["med"], 3),
                  scene=report.get("scene"))

    report["wb_gain"] = _auto_white_balance(a, p["wb"], p["wb_max"])
    report["exposure_stops"] = _auto_exposure(a, p["target"], p["strength"])
    report["shadow_lift"], report["highlight_recovery"] = _recover_tones(a, a[::4, ::4] @ LUMA)
    if style == "mono":
        _levels(a, 1.0, 0.4, 99.6, 0.015, 0.975)         # rich blacks, detailed whites
    else:
        _levels(a, p["levels"])

    skin = _skin_full(a)
    mask = None
    if p.get("flash") or p.get("separation"):
        mask = _subject_mask((a.shape[1], a.shape[0]), faces, pt)

    a *= p["exposure"]
    np.clip(a, 0, 1, out=a)
    if p.get("mix"):
        _mono(a, st)
    _contrast(a, p["contrast"])
    if not p.get("mix"):
        _saturate(a, p["saturation"], skin)
        a[..., 0] += p["warmth"]
        a[..., 2] -= p["warmth"]
        _split_tone(a, p["shadows"], p["highlights"], skin)
    f = p["fade"]
    if f > 0:
        a *= 1 - f
        a += f
    np.clip(a, 0, 1, out=a)

    if p.get("flash"):
        _flash(a, mask, p["flash"])
    if p.get("separation"):
        _separate(a, mask, p["separation"])
    if p.get("sky"):
        _sky_balance(a, p["sky"])
    if p.get("cast_cap"):
        report["rb_ratio_before_guard"] = _cast_guard(a, p["cast_cap"])
    _clarity(a, p["clarity"], skin)
    del skin
    _bloom(a, p["bloom"], p["bloom_tint"])
    _vignette(a, p["vignette"])
    if p.get("ab"):
        _chromatic(a, p["ab"])
    _grain(a, p["grain"], p["grain_px"], rng, p.get("chroma_noise", 0.0))
    if p.get("mix"):                                     # keep it strictly neutral
        g = a.mean(-1)
        a[...] = g[..., None]

    np.clip(a, 0, 1, out=a)
    a *= 255.0
    out = Image.fromarray(a.astype(np.uint8))
    del a
    radius = max(1.0, 0.0012 * max(out.size))
    return out.filter(ImageFilter.UnsharpMask(radius=radius, percent=int(p["sharpen"]),
                                              threshold=2))


# ---------------------------------------------------------------------------
# 6. Text overlay (only when text is passed)
# ---------------------------------------------------------------------------
def _font(kind, size):
    files = {
        "light": ["Poppins-Light.ttf"], "medium": ["Poppins-Medium.ttf"],
        "bold": ["Poppins-Bold.ttf"], "serif": ["Lora-Italic-Variable.ttf"],
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
    text = "".join(ch for ch in str(text) if ord(ch) < 0x2070)   # emoji -> boxes
    return re.sub(r"\s+", " ", text).strip()


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
    w, h = img.size
    band = int(h * 0.38)
    ramp = np.linspace(0, 1, band, dtype=np.float32) ** 1.8
    alpha = (ramp * 0.50 * 255).astype(np.uint8)
    if position == "top":
        alpha = alpha[::-1]
    layer = Image.new("RGBA", (w, band), (0, 0, 0, 0))
    layer.putalpha(Image.fromarray(np.tile(alpha[:, None], (1, w))))
    out = img.convert("RGBA")
    out.alpha_composite(layer, (0, 0 if position == "top" else h - band))
    return out


def _hits_face(box, faces, w, h):
    l, t, r, b = box
    for x, y, fw, fh in faces:
        fl, ft = (x - 0.04) * w, (y - 0.04) * h
        fr, fb = (x + fw + 0.04) * w, (y + fh + 0.04) * h
        if l < fr and r > fl and t < fb and b > ft:
            return True
    return False


def _add_text(img, text, style="minimal", position="bottom", is_story=False, faces=(),
              report=None):
    text = _clean_text(text)
    if not text:
        return img
    kind, size_frac, upper, track_em = TEXT_STYLES.get(style, TEXT_STYLES["minimal"])
    if upper:
        text = text.upper()
    w, h = img.size
    font = _font(kind, int(w * size_frac))
    tracking_px = font.size * track_em
    lines = _wrap(text, font, tracking_px, w * 0.80)[:4]
    line_h = int(font.size * 1.28)
    block_h = line_h * len(lines)
    block_w = max(_text_width(ln, font, tracking_px) for ln in lines)
    top_safe = int(h * (0.14 if is_story else 0.07))
    bottom_safe = int(h * (0.20 if is_story else 0.08))

    def y_for(pos):
        if pos == "top":
            return top_safe
        if pos == "center":
            return (h - block_h) // 2
        return h - bottom_safe - block_h

    order = [position] + [p for p in ("bottom", "top") if p != position]
    chosen = None
    for pos in order:                                    # never print over a face
        y = y_for(pos)
        box = ((w - block_w) / 2, y, (w + block_w) / 2, y + block_h)
        if not _hits_face(box, faces, w, h):
            chosen = pos
            break
    if chosen is None:
        if report is not None:
            report["text"] = "skipped_faces"
        return img
    position, y = chosen, y_for(chosen)
    if report is not None:
        report["text"] = position

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
    tags=None,
    faces=None,
    seed=None,
    adaptive_plan=None,
):
    """Return a finished JPEG as BytesIO (or (BytesIO, report)).

    style  natural(auto) | y2k | moody | film | clean | golden | mono
           (old names baddie/soft/glow/bw/... still work)
    size   post (4:5) | story (9:16) | square | original (<= 1800 px)
    tags   whatever the tagger server sent: ["night","party"], "glow",
           {"style": "film", "scene": ["travel"]} ... (optional)
    faces  [[x, y, w, h], ...] top-left, 0..1 of the ORIGINAL photo (optional,
           e.g. from the tagger). Faces are never cropped; without them the
           framing is conservative and OpenCV is used if installed.
    text   printed ONLY when given (no text = no text); avoids faces.
    seed   any str/int; the per-photo jitter is derived from the image anyway.
    """
    report = {}
    img = _load(image_bytes, size)
    faces = _clean_faces(faces)
    if not faces and cv2 is not None:
        faces = _detect_faces_cv(img)

    if size in SIZES:
        img, faces = _fit(img, SIZES[size], faces, report)
    else:
        report["crop"] = "none"
    skin, pt = _quick_look(img)

    digest = hashlib.blake2b(image_bytes[:65536] + str(seed or "").encode(),
                             digest_size=8).digest()
    rng = np.random.default_rng(int.from_bytes(digest, "little"))

    requested = resolve_style(style)
    tweaks = None
    scene = None
    if requested == "natural":
        small = img.copy()
        small.thumbnail((200, 200))
        stats = _measure(np.asarray(small, dtype=np.float32) / 255.0, 0.0)
        requested, scene = choose_style(stats, tags)
        report["auto"] = True
    else:                                                # an explicit look + scene hints
        words = _flat_tags(tags)
        for wd in SCENES:
            if wd in words:
                scene = wd
                break
    if scene:
        tweaks = SCENES[scene][1]
        report["scene"] = scene
    if requested not in STYLES:
        requested = "clean"

    img = _style_pass(img, requested, tweaks, faces, pt, rng, report,
                      adaptive_plan=adaptive_plan)
    gc.collect()

    if text:
        img = _add_text(img, text, text_style, text_position,
                        is_story=(size == "story"), faces=faces, report=report)

    out = io.BytesIO()
    try:
        img.save(out, format="JPEG", quality=93, subsampling=0, optimize=True)
    except OSError:                       # very noisy frames can overflow the optimiser buffer
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=93, subsampling=0)
    out.seek(0)
    report = {k: v for k, v in report.items() if v is not None}
    return (out, report) if return_report else out
