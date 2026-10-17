"""photo_intel.py - deep, local, zero-cost photo intelligence (pure: no DB, no network).

This is the first stage of the "Analyze & Score -> Shortlist" step. It measures far
more than the old brightness/blur check and runs in a few hundred milliseconds per
photo, so even a 300-photo bulk upload is analysed before a single paid AI call.

deep_analyze(img) returns a JSON-safe dict with:
  identity   phash (DCT perceptual hash), hist (64-bin colour signature)
  people     faces (count, size, sharpness, eyes/smile guess, cut-off), when OpenCV exists
  technical  subject_sharpness (sharpness where it matters), motion_blur, noise, resolution
  exposure   percentiles, dynamic range, clipping, face exposure, colour cast
  look       colourfulness, saturation
  framing    saliency centre of mass, rule-of-thirds fit, subject clarity,
             border clutter, 4:5 and 9:16 crop safety
  safety     screenshot likelihood, tiny / blank-frame detection
  scores     technical / exposure / composition / color / subject  (0-100 each)
  local_score  final 0-100, flags (names match photo_judge.FLAG_PENALTIES)

Everything degrades gracefully: without OpenCV the face signals are simply missing
(never "zero faces"), and any internal failure returns None so the caller keeps the
old behaviour.
"""
from __future__ import annotations

import math
import os
from typing import Any

import numpy as np
from PIL import Image, ImageFilter

_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)
SIDE = 512                      # main measurement size (same as media_quality)
MID_SIDE = 1024                 # noise is measured at near-native detail
HIST_BINS = 4                   # 4x4x4 = 64 colour bins

# Hard-junk limits. Deliberately extreme: only photos that can never be posted.
TINY_SHORT_SIDE = 320
BLANK_LOW, BLANK_HIGH = 12.0, 244.0     # mean luma of an all-black / all-white frame
SCREENSHOT_REJECT = 0.85
_PHONE_SCREEN_WIDTHS = {640, 720, 750, 828, 1080, 1125, 1170, 1179, 1242, 1284, 1290, 1440}


def strict_junk_enabled() -> bool:
    return os.environ.get("VIBEOS_STRICT_JUNK", "1").strip().lower() not in ("0", "false", "off", "no")


def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, float(v)))


# ----------------------------------------------------------------------------
# Hashes / signatures
# ----------------------------------------------------------------------------
def _dct_matrix(n: int) -> np.ndarray:
    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    m = np.cos(np.pi * (2 * i + 1) * k / (2 * n)) * math.sqrt(2.0 / n)
    m[0] /= math.sqrt(2.0)
    return m.astype(np.float64)


_DCT32 = _dct_matrix(32)


def phash(img: Image.Image) -> str:
    """64-bit DCT perceptual hash (16 hex). Far more robust than dHash to
    re-compression, resizing, light edits and small crops."""
    g = np.asarray(img.convert("L").resize((32, 32), Image.LANCZOS), dtype=np.float64)
    low = (_DCT32 @ g @ _DCT32.T)[:8, :8]
    flat = low.flatten()
    med = np.median(flat[1:])
    value = 0
    for b in (flat > med):
        value = (value << 1) | int(b)
    return f"{value:016x}"


def colour_signature(arr_u8: np.ndarray) -> list[int]:
    """64-bin RGB histogram, stored as ints summing to ~1000 (compact in Mongo)."""
    q = (arr_u8.astype(np.int32) * HIST_BINS // 256).reshape(-1, 3)
    idx = q[:, 0] * HIST_BINS * HIST_BINS + q[:, 1] * HIST_BINS + q[:, 2]
    h = np.bincount(idx, minlength=HIST_BINS ** 3).astype(np.float64)
    h = h / max(1.0, h.sum())
    return [int(round(x * 1000)) for x in h]


def hist_similarity(a, b) -> float | None:
    """Histogram intersection in 0..1 (1 = same colour make-up)."""
    if not a or not b or len(a) != len(b):
        return None
    sa, sb = float(sum(a)) or 1.0, float(sum(b)) or 1.0
    return float(sum(min(x / sa, y / sb) for x, y in zip(a, b)))


def hamming_hex(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    return bin(int(a, 16) ^ int(b, 16)).count("1")


# ----------------------------------------------------------------------------
# Low-level measurements
# ----------------------------------------------------------------------------
def _laplacian(gray: np.ndarray) -> np.ndarray:
    c = gray[1:-1, 1:-1]
    return 4 * c - gray[:-2, 1:-1] - gray[2:, 1:-1] - gray[1:-1, :-2] - gray[1:-1, 2:]


def _lap_var(gray: np.ndarray) -> float:
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0
    return float(_laplacian(gray).var())


def _tile_sharpness_map(gray: np.ndarray, grid: int = 4):
    h, w = gray.shape
    th, tw = max(h // grid, 3), max(w // grid, 3)
    out = np.zeros((grid, grid), dtype=np.float64)
    for gy in range(grid):
        for gx in range(grid):
            y, x = gy * th, gx * tw
            out[gy, gx] = _lap_var(gray[y:y + th, x:x + tw])
    return out


def _noise_sigma(gray_mid: np.ndarray) -> float:
    """Robust noise std (Immerkaer filter + median/MAD, so edges don't count)."""
    h, w = gray_mid.shape
    if h < 64 or w < 64:
        return 0.0
    ch, cw = min(h, 512), min(w, 512)
    y0, x0 = (h - ch) // 2, (w - cw) // 2
    g = gray_mid[y0:y0 + ch, x0:x0 + cw]
    c = (g[:-2, :-2] - 2 * g[:-2, 1:-1] + g[:-2, 2:]
         - 2 * g[1:-1, :-2] + 4 * g[1:-1, 1:-1] - 2 * g[1:-1, 2:]
         + g[2:, :-2] - 2 * g[2:, 1:-1] + g[2:, 2:])
    sigma = float(np.median(np.abs(c)) / (0.6745 * 6.0))
    return sigma


def _directional_ratio(gray: np.ndarray) -> float:
    """min/max directional gradient energy. Real motion blur kills one direction."""
    if min(gray.shape) < 8:
        return 1.0
    gx = np.diff(gray, axis=1)[:-1, :]
    gy = np.diff(gray, axis=0)[:, :-1]
    ex, ey = float((gx ** 2).mean()), float((gy ** 2).mean())
    hi = max(ex, ey)
    return 1.0 if hi < 1e-6 else min(ex, ey) / hi


def _box3(a: np.ndarray) -> np.ndarray:
    p = np.pad(a, 1, mode="edge")
    return sum(p[dy:dy + a.shape[0], dx:dx + a.shape[1]]
               for dy in range(3) for dx in range(3)) / 9.0


def _saliency(small_rgb_u8: np.ndarray, faces: list[dict] | None) -> np.ndarray:
    """Spectral-residual saliency (64x64, sums to 1), boosted on faces and saturation."""
    side = 64
    img = Image.fromarray(small_rgb_u8).resize((side, side), Image.BILINEAR)
    arr = np.asarray(img, dtype=np.float32)
    gray = arr @ _LUMA
    f = np.fft.fft2(gray)
    amp, phase = np.abs(f), np.angle(f)
    log_amp = np.log(amp + 1e-8)
    residual = log_amp - _box3(log_amp)
    sal = np.abs(np.fft.ifft2(np.exp(residual + 1j * phase))) ** 2
    sal = np.asarray(Image.fromarray(
        (255 * sal / (sal.max() + 1e-9)).astype(np.uint8)).filter(ImageFilter.GaussianBlur(2)),
        dtype=np.float32) / 255.0
    sat = (arr.max(axis=2) - arr.min(axis=2)) / 255.0
    sal = 0.75 * sal + 0.25 * sat * sal.max()
    for f_ in faces or []:
        x0, y0 = int(f_["x"] * side), int(f_["y"] * side)
        x1, y1 = int((f_["x"] + f_["w"]) * side) + 1, int((f_["y"] + f_["h"]) * side) + 1
        sal[y0:y1, x0:x1] += 1.2 * sal.max() + 0.2
    sal = sal + 1e-4
    return (sal / sal.sum()).astype(np.float64)


def _crop_retained(sal: np.ndarray, aspect_wh: float, img_aspect: float) -> float:
    """Best-case saliency mass kept when the frame is cropped to aspect_wh (w/h)."""
    side = sal.shape[0]
    if abs(img_aspect - aspect_wh) < 0.02:
        return 1.0
    best = 0.0
    if img_aspect > aspect_wh:                       # too wide: crop columns
        keep = max(1, int(round(side * aspect_wh / img_aspect)))
        csum = np.convolve(sal.sum(axis=0), np.ones(keep), mode="valid")
        best = float(csum.max())
    else:                                            # too tall: crop rows
        keep = max(1, int(round(side * img_aspect / aspect_wh)))
        rsum = np.convolve(sal.sum(axis=1), np.ones(keep), mode="valid")
        best = float(rsum.max())
    return _clamp(best)


# ----------------------------------------------------------------------------
# Faces (OpenCV Haar, optional)
# ----------------------------------------------------------------------------
_CASCADES: dict[str, Any] = {}


def _cascade(name: str):
    try:
        import cv2
    except Exception:
        return None
    if name not in _CASCADES:
        try:
            c = cv2.CascadeClassifier(cv2.data.haarcascades + name)
            _CASCADES[name] = None if c.empty() else c
        except Exception:
            _CASCADES[name] = None
    return _CASCADES[name]


def detect_faces(rgb_u8: np.ndarray) -> list[dict] | None:
    """Return faces (normalised boxes + quality hints) or None when unavailable."""
    face_c = _cascade("haarcascade_frontalface_default.xml")
    if face_c is None:
        return None
    import cv2
    gray = cv2.cvtColor(rgb_u8, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    eq = cv2.equalizeHist(gray)
    min_px = max(32, int(0.07 * min(h, w)))
    try:
        found = face_c.detectMultiScale(eq, scaleFactor=1.1, minNeighbors=6,
                                        minSize=(min_px, min_px))
    except Exception:
        return None
    eye_c = _cascade("haarcascade_eye.xml")
    smile_c = _cascade("haarcascade_smile.xml")
    faces = []
    for (x, y, fw, fh) in sorted(found, key=lambda r: -r[2] * r[3])[:6]:
        crop_g = gray[y:y + fh, x:x + fw].astype(np.float32)
        info = {
            "x": round(x / w, 4), "y": round(y / h, 4),
            "w": round(fw / w, 4), "h": round(fh / h, 4),
            "area": round((fw * fh) / (w * h), 4),
            "sharp": round(_lap_var(crop_g), 1),
            "luma": round(float(crop_g.mean()), 1),
            "eyes": None, "smile": None,
            "cut_off": bool(x <= 1 or y <= 1 or x + fw >= w - 1 or y + fh >= h - 1),
        }
        if eye_c is not None and fw >= 60:
            upper = eq[y:y + int(fh * 0.6), x:x + fw]
            try:
                eyes = eye_c.detectMultiScale(upper, scaleFactor=1.1, minNeighbors=5,
                                              minSize=(max(8, fw // 10), max(8, fw // 10)))
                info["eyes"] = int(len(eyes))
            except Exception:
                pass
        if smile_c is not None and fw >= 80:
            lower = eq[y + int(fh * 0.55):y + fh, x:x + fw]
            try:
                sm = smile_c.detectMultiScale(lower, scaleFactor=1.7, minNeighbors=22,
                                              minSize=(max(10, fw // 5), max(6, fw // 12)))
                info["smile"] = bool(len(sm))
            except Exception:
                pass
        faces.append(info)
    return faces


# ----------------------------------------------------------------------------
# Screenshot / junk detection
# ----------------------------------------------------------------------------
def screenshot_likelihood(img: Image.Image, width: int, height: int) -> float:
    thumb = img.copy()
    thumb.thumbnail((256, 256), Image.BILINEAR)
    a = np.asarray(thumb, dtype=np.uint8)
    h, w = a.shape[:2]
    if h < 16 or w < 16:
        return 0.0
    gray = a.astype(np.float32) @ _LUMA
    bh, bw = h // 8, w // 8
    blocks = gray[:bh * 8, :bw * 8].reshape(bh, 8, bw, 8).std(axis=(1, 3))
    flat_frac = float((blocks < 0.6).mean())
    flat_rgb = a.reshape(-1, 3)
    unique = len(np.unique(flat_rgb[:, 0].astype(np.int32) * 65536
                           + flat_rgb[:, 1].astype(np.int32) * 256 + flat_rgb[:, 2]))
    unique_ratio = unique / float(h * w)
    q = (a.astype(np.int32) >> 3).reshape(-1, 3)
    top_share = float(np.bincount(q[:, 0] * 1024 + q[:, 1] * 32 + q[:, 2]).max()) / q.shape[0]
    s1 = _clamp((flat_frac - 0.25) / 0.5)
    s2 = _clamp((0.12 - unique_ratio) / 0.12)
    s3 = _clamp((top_share - 0.15) / 0.4)
    score = 0.4 * s1 + 0.35 * s2 + 0.25 * s3
    aspect = width / max(1, height)
    if width in _PHONE_SCREEN_WIDTHS and (aspect < 0.56 or aspect > 1.78):
        score += 0.2
    return round(_clamp(score), 3)


# ----------------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------------
def deep_analyze(img: Image.Image) -> dict | None:
    """Full local analysis. Returns None if anything goes wrong (caller falls back)."""
    try:
        return _deep_analyze(img)
    except Exception:
        return None


def _deep_analyze(img: Image.Image) -> dict:
    width, height = img.size
    aspect = width / max(1, height)

    mid = img.copy()
    mid.thumbnail((MID_SIDE, MID_SIDE), Image.LANCZOS)
    gray_mid = np.asarray(mid, dtype=np.float32) @ _LUMA

    small = img.copy()
    small.thumbnail((SIDE, SIDE), Image.LANCZOS)
    rgb_u8 = np.asarray(small, dtype=np.uint8)
    rgb = rgb_u8.astype(np.float32)
    gray = rgb @ _LUMA
    sh, sw = gray.shape

    # ---- identity ---------------------------------------------------------
    ph = phash(img)
    hist = colour_signature(rgb_u8)

    # ---- faces & saliency ---------------------------------------------------
    faces = detect_faces(rgb_u8)
    sal = _saliency(rgb_u8, faces)
    sal_big = np.asarray(Image.fromarray(
        (255 * sal / sal.max()).astype(np.uint8)).resize((sw, sh), Image.BILINEAR),
        dtype=np.float64)

    # ---- sharpness where it matters --------------------------------------------
    tile = _tile_sharpness_map(gray, 4)
    global_sharp = float(np.percentile(tile, 90))
    sal_tiles = np.asarray(Image.fromarray(
        (255 * sal / sal.max()).astype(np.uint8)).resize((4, 4), Image.BOX), dtype=np.float64)
    weight = sal_tiles / max(1e-9, sal_tiles.sum())
    focus_sharp = float((tile * weight).sum())
    if faces and faces[0]["area"] >= 0.02:
        subject_sharp = 0.6 * float(faces[0]["sharp"]) + 0.4 * global_sharp
    else:
        subject_sharp = max(focus_sharp, global_sharp * 0.8)

    thresh = np.percentile(sal_big, 85)
    ys, xs = np.where(sal_big >= thresh)
    if len(ys) > 200:
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        region = gray[y0:y1, x0:x1]
    else:
        region = gray
    dir_ratio = _directional_ratio(region)
    motion_blur = bool(subject_sharp < 90 and dir_ratio < 0.40)

    noise = _noise_sigma(gray_mid)

    # ---- exposure ------------------------------------------------------------
    p1, p5, p50, p95, p99 = (float(v) for v in np.percentile(gray, [1, 5, 50, 95, 99]))
    brightness = float(gray.mean())
    contrast = float(gray.std())
    dyn_range = p99 - p1
    clip_lo = float((gray < 8).mean())
    clip_hi = float((gray > 247).mean())
    hist_l = np.bincount(gray.astype(np.uint8).ravel(), minlength=256).astype(np.float64)
    hist_l /= hist_l.sum()
    entropy = float(-(hist_l[hist_l > 0] * np.log2(hist_l[hist_l > 0])).sum())

    mid_mask = (gray > 60) & (gray < 200)
    if mid_mask.sum() > 500:
        mr, mg, mb = (float(rgb[..., i][mid_mask].mean()) for i in range(3))
        cast = max(abs(mr - mg), abs(mb - mg)) / max(1.0, (mr + mg + mb) / 3.0)
    else:
        cast = 0.0

    # ---- colour ----------------------------------------------------------------
    rg = rgb[..., 0] - rgb[..., 1]
    yb = 0.5 * (rgb[..., 0] + rgb[..., 1]) - rgb[..., 2]
    colourfulness = math.sqrt(float(rg.std()) ** 2 + float(yb.std()) ** 2) \
        + 0.3 * math.sqrt(float(rg.mean()) ** 2 + float(yb.mean()) ** 2)
    saturation = float(((rgb.max(axis=2) - rgb.min(axis=2)) / 255.0).mean())

    # ---- framing ---------------------------------------------------------------
    gy_idx, gx_idx = np.mgrid[0:sal.shape[0], 0:sal.shape[1]]
    cx = float((sal * (gx_idx + 0.5)).sum() / sal.shape[1])
    cy = float((sal * (gy_idx + 0.5)).sum() / sal.shape[0])
    thirds = [(1 / 3, 1 / 3), (2 / 3, 1 / 3), (1 / 3, 2 / 3), (2 / 3, 2 / 3)]
    d_third = min(math.hypot(cx - px, cy - py) for px, py in thirds)
    d_center = math.hypot(cx - 0.5, cy - 0.5)
    pos = max(math.exp(-(d_third / 0.17) ** 2), 0.9 * math.exp(-(d_center / 0.17) ** 2))
    flat_sorted = np.sort(sal.ravel())[::-1]
    top_mass = float(flat_sorted[:max(1, int(0.15 * flat_sorted.size))].sum())
    clarity = _clamp((top_mass - 0.28) / 0.45)             # concentrated = clear subject
    bm = max(1, int(round(0.12 * sal.shape[0])))
    border = np.ones_like(sal, dtype=bool)
    border[bm:-bm, bm:-bm] = False
    border_share = float(sal[border].sum()) / max(1e-9, float(border.mean()))
    border_clutter = _clamp((border_share - 1.0) / 1.2)   # >1 = border busier than average
    fit_post = _crop_retained(sal, 0.8, aspect)
    fit_story = _crop_retained(sal, 9 / 16, aspect)

    face_cut = bool(faces and any(f["cut_off"] and f["area"] > 0.01 for f in faces))
    # a face sliced by the crop is worse than saliency alone says
    def _face_safe(ratio):
        if not faces:
            return True
        for f in faces:
            if f["area"] < 0.01:
                continue
            fx0, fx1 = f["x"], f["x"] + f["w"]
            fy0, fy1 = f["y"], f["y"] + f["h"]
            if aspect > ratio:                       # crop columns
                kw = ratio / aspect
                lo = _clamp(((fx0 + fx1) / 2) - kw / 2, 0, 1 - kw)
                if fx0 < lo - 0.01 or fx1 > lo + kw + 0.01:
                    return False
            else:
                kh = aspect / ratio
                lo = _clamp(((fy0 + fy1) / 2) - kh / 2, 0, 1 - kh)
                if fy0 < lo - 0.01 or fy1 > lo + kh + 0.01:
                    return False
        return True
    post_face_ok, story_face_ok = _face_safe(0.8), _face_safe(9 / 16)

    # ---- junk / safety ------------------------------------------------------------
    shot = screenshot_likelihood(img, width, height)
    junk = []
    if min(width, height) < TINY_SHORT_SIDE:
        junk.append("tiny")
    if brightness < BLANK_LOW or brightness > BLANK_HIGH:
        junk.append("blank_frame")
    if shot >= SCREENSHOT_REJECT:
        junk.append("screenshot")

    # ---- sub-scores (0-100) -----------------------------------------------------------
    s_sharp = _clamp(subject_sharp / 220.0) * 100
    s_noise = _clamp(1 - noise / 7.0) * 100
    s_res = _clamp(min(width, height) / 1080.0) * 100
    technical = 0.55 * s_sharp + 0.20 * s_noise + 0.25 * s_res
    if motion_blur:
        technical -= 15

    s_med = _clamp(1 - abs(p50 - 120) / 95.0)
    s_dyn = _clamp((dyn_range - 80) / 120.0)
    s_clip = _clamp(1 - (clip_lo + clip_hi) * 4)
    exposure = 100 * (0.5 * s_med + 0.3 * s_dyn + 0.2 * s_clip)
    if faces and faces[0]["area"] >= 0.02:
        fl = faces[0]["luma"]
        exposure = 0.75 * exposure + 0.25 * 100 * _clamp(1 - abs(fl - 135) / 100.0)

    face_pen = 0.0
    face_bonus = 0.0
    if faces:
        main = faces[0]
        if main["cut_off"]:
            face_pen += 18
        if not (post_face_ok and story_face_ok):
            face_pen += 8
        if 0.03 <= main["area"] <= 0.45:
            face_bonus += 8
    comp = 100 * (0.32 * pos + 0.26 * clarity + 0.22 * (0.6 * fit_post + 0.4 * fit_story)
                  + 0.20 * (1 - border_clutter))
    composition = comp + face_bonus - face_pen

    color = 100 * (0.75 * _clamp((colourfulness - 8) / 45.0) + 0.25 * _clamp(1 - cast / 0.25))
    color = max(color, 30.0)

    eyes_closed_suspect = bool(
        faces and faces[0]["eyes"] == 0 and faces[0]["area"] >= 0.04)
    if faces:
        main = faces[0]
        fq = 55.0
        fq += 18 * _clamp(main["sharp"] / 220.0)
        fq += 12 if main["eyes"] and main["eyes"] >= 1 else 0
        fq += 8 if main["smile"] else 0
        fq -= 25 if eyes_closed_suspect else 0
        fq -= 15 if main["cut_off"] else 0
        subject = _clamp(fq / 100.0) * 100
    else:
        subject = 100 * (0.5 + 0.5 * clarity) * 0.9

    flags = []
    if subject_sharp < 38:
        flags.append("blurry")
    if motion_blur:
        flags.append("motion_blur")
    if p50 < 55:
        flags.append("too_dark")
    if p50 > 205 or clip_hi > 0.22:
        flags.append("overexposed")
    if face_cut:
        flags.append("subject_cut_off")
    if border_clutter > 0.65 and clarity < 0.35:
        flags.append("cluttered_background")
    if shot >= 0.55:
        flags.append("screenshot_or_screen_photo")
    if min(width, height) < 720:
        flags.append("low_res_looking")
    if noise > 5.5:
        flags.append("noisy")
    if eyes_closed_suspect:
        flags.append("eyes_closed")

    scores = {k: int(round(max(0.0, min(100.0, v)))) for k, v in (
        ("technical", technical), ("exposure", exposure), ("composition", composition),
        ("color", color), ("subject", subject))}
    penalty = {"blurry": 14, "motion_blur": 6, "too_dark": 8, "overexposed": 8,
               "subject_cut_off": 6, "cluttered_background": 4,
               "screenshot_or_screen_photo": 30, "low_res_looking": 6, "noisy": 5,
               "eyes_closed": 6}
    local = (0.30 * scores["technical"] + 0.20 * scores["exposure"]
             + 0.24 * scores["composition"] + 0.10 * scores["color"]
             + 0.16 * scores["subject"]) - sum(penalty[f] for f in flags)
    local_score = int(round(max(0.0, min(100.0, local))))

    return {
        "version": 2,
        "phash": ph,
        "hist": hist,
        "faces": None if faces is None else [
            {k: v for k, v in f.items() if k in ("area", "sharp", "eyes", "smile", "cut_off")}
            for f in faces],
        "face_count": None if faces is None else len(faces),
        "subject_sharpness": round(subject_sharp, 1),
        "global_sharpness": round(global_sharp, 1),
        "motion_blur": motion_blur,
        "noise": round(noise, 2),
        "p1": round(p1, 1), "p50": round(p50, 1), "p99": round(p99, 1),
        "dynamic_range": round(dyn_range, 1),
        "entropy": round(entropy, 2),
        "brightness": round(brightness, 1),
        "contrast": round(contrast, 1),
        "clipped_shadows": round(clip_lo, 3), "clipped_highlights": round(clip_hi, 3),
        "colorfulness": round(colourfulness, 1),
        "saturation": round(saturation, 3),
        "color_cast": round(cast, 3),
        "saliency_center": [round(cx, 3), round(cy, 3)],
        "thirds_fit": round(pos, 3),
        "subject_clarity": round(clarity, 3),
        "border_clutter": round(border_clutter, 3),
        "crop_fit_post": round(fit_post, 3),
        "crop_fit_story": round(fit_story, 3),
        "screenshot_likelihood": shot,
        "junk": junk,
        "scores": scores,
        "flags": flags,
        "local_score": local_score,
    }


def burst_rank(deep: dict | None, base_score: float) -> float:
    """Tie-break value for choosing the best frame of a burst / near-duplicate group.

    Photos of the same moment differ mainly in blinks, smiles and focus, so those
    count extra here (and only here)."""
    if not deep:
        return float(base_score or 0)
    v = float(base_score or 0)
    faces = deep.get("faces") or []
    if faces:
        main = faces[0]
        if main.get("eyes") and main["eyes"] >= 1:
            v += 4
        elif main.get("eyes") == 0 and main.get("area", 0) >= 0.04:
            v -= 8
        if main.get("smile"):
            v += 3
    v += 0.04 * min(250.0, float(deep.get("subject_sharpness") or 0))
    return v
