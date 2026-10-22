"""7 measurements from a photo using OpenCV only (no AI API, no network)."""
from __future__ import annotations

import cv2
import numpy as np

MAX_SIDE = 768  # fixed analysis size: fast, and makes grain/noise comparable
MAX_FACES = 8
_CASCADES: dict = {}


def decode(data: bytes) -> np.ndarray:
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Not a readable image")
    h, w = img.shape[:2]
    scale = MAX_SIDE / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return img


def _noise_sigma(gray: np.ndarray) -> float:
    """Immerkaer fast noise estimate (std-dev in 0-255 units).

    Unlike Laplacian variance, this ignores real edges, so a sharp photo is not
    mistaken for a noisy one and a blurry photo is not mistaken for a grainy one.
    """
    k = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], dtype=np.float32)
    conv = cv2.filter2D(gray.astype(np.float32), -1, k)
    h, w = gray.shape
    return float(np.sqrt(np.pi / 2) * np.abs(conv[1:-1, 1:-1]).sum() / (6.0 * (w - 2) * (h - 2)))


def _cascade(name: str):
    """Haar cascades ship inside opencv-python-headless (no download, no network)."""
    if name not in _CASCADES:
        try:
            c = cv2.CascadeClassifier(cv2.data.haarcascades + name)
            _CASCADES[name] = c if not c.empty() else None
        except Exception:
            _CASCADES[name] = None
    return _CASCADES[name]


def _iou(a, b) -> float:
    ax0, ay0, ax1, ay1 = a[0], a[1], a[0] + a[2], a[1] + a[3]
    bx0, by0, bx1, by1 = b[0], b[1], b[0] + b[2], b[1] + b[3]
    iw, ih = min(ax1, bx1) - max(ax0, bx0), min(ay1, by1) - max(ay0, by0)
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    return inter / (a[2] * a[3] + b[2] * b[3] - inter)


def detect_faces(gray: np.ndarray) -> list[list[float]]:
    """Frontal + left/right profile faces -> [[x, y, w, h], ...] normalised 0..1 (top-left).

    The backend editor uses these so it never crops a face and knows where to
    retouch. Haar is not perfect: min_neighbors is strict, so it prefers missing a
    small face over inventing one.
    """
    h, w = gray.shape
    eq = cv2.equalizeHist(gray)
    m = max(24, int(min(h, w) * 0.06))
    found: list[tuple] = []
    front = _cascade("haarcascade_frontalface_default.xml")
    if front is not None:
        for (x, y, fw, fh) in front.detectMultiScale(eq, 1.1, 6, minSize=(m, m)):
            found.append((x / w, y / h, fw / w, fh / h))
    prof = _cascade("haarcascade_profileface.xml")
    if prof is not None:
        for (x, y, fw, fh) in prof.detectMultiScale(eq, 1.1, 6, minSize=(m, m)):
            found.append((x / w, y / h, fw / w, fh / h))
        flipped = cv2.flip(eq, 1)
        for (x, y, fw, fh) in prof.detectMultiScale(flipped, 1.1, 6, minSize=(m, m)):
            found.append((1.0 - (x + fw) / w, y / h, fw / w, fh / h))
    found.sort(key=lambda f: f[2] * f[3], reverse=True)
    kept: list[tuple] = []
    for f in found:
        if all(_iou(f, k) < 0.3 for k in kept):
            kept.append(f)
    return [[round(v, 4) for v in f] for f in kept[:MAX_FACES]]


def content_features(img: np.ndarray, gray: np.ndarray, hsv: np.ndarray) -> dict:
    """What is IN the photo (not just its tone): faces, skin, flash, sky."""
    faces = []
    try:
        faces = detect_faces(gray)
    except Exception:
        faces = []
    ycc = cv2.cvtColor(img, cv2.COLOR_BGR2YCrCb)
    yy, cr, cb = ycc[:, :, 0], ycc[:, :, 1], ycc[:, :, 2]
    skin = (cr >= 133) & (cr <= 173) & (cb >= 77) & (cb <= 127) & (yy > 18)
    h, w = gray.shape
    cy0, cy1, cx0, cx1 = h // 4, 3 * h // 4, w // 4, 3 * w // 4
    centre = gray[cy0:cy1, cx0:cx1]
    edge_sum = float(gray.sum()) - float(centre.sum())
    edge_n = max(1, gray.size - centre.size)
    top = img[: max(1, h // 3)]
    b, g, r = top[:, :, 0].astype(np.int16), top[:, :, 1].astype(np.int16), top[:, :, 2].astype(np.int16)
    sky = float(((b > r + 8) & (b > g)).mean())
    return {
        "n_faces": len(faces),
        "face_area": round(max((f[2] * f[3] for f in faces), default=0.0), 4),
        "skin_share": round(float(skin.mean()), 3),
        "flashy": bool(float(centre.mean()) - edge_sum / edge_n > 38.0),
        "sky": round(sky, 3),
        "_faces": faces,
    }


def measure(data: bytes) -> dict:
    img = decode(data)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    b, g, r = cv2.split(img)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    mean_l = float(gray.mean())
    std_l = float(gray.std())
    p5, p10, p90, p95 = (float(np.percentile(gray, p)) for p in (5, 10, 90, 95))
    sat_mean = float(hsv[:, :, 1].mean())
    warm_raw = (float(r.mean()) - float(b.mean())) / 1.5
    noise = _noise_sigma(gray)

    values = {  # the 7 table parameters, as measured on the photo
        "exposure": (mean_l - 128) / 1.28,
        "contrast": (std_l - 50) * 1.8,
        "highlights": p90 - 190,
        "shadows": p10 - 50,
        "saturation": sat_mean - 90,
        "warmth": warm_raw,
        "grain": noise * 12,           # sigma 0->0, ~3 (visible grain)->36, ~8->96
    }
    values = {k: int(np.clip(v, 0 if k == "grain" else -100, 100)) for k, v in values.items()}

    extras = {  # helpers for style matching, not part of the 7
        "sat_mean": round(sat_mean, 1),
        "p5": round(p5, 1), "p95": round(p95, 1),
        "dark_ratio": round(float((gray < 40).mean()), 3),
        "bright_ratio": round(float((gray > 215).mean()), 3),
        "noise_sigma": round(noise, 2),
    }
    content = content_features(img, gray, hsv)
    faces = content.pop("_faces")
    return {"values": values, "extras": extras, "content": content, "faces": faces,
            "size": list(img.shape[1::-1])}
