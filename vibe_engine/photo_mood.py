"""Photo-first scene understanding: mood, faces, busy-ness and empty space.

No generic backgrounds here - the photo IS the background. We only learn
where the calm space is (for caption placement) and where faces are (never
cover them with text or stickers).
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, ImageStat

_YUNET = {"det": None, "tried": False}
_YUNET_FILE = "face_detection_yunet_2023mar.onnx"


def _yunet():
    """Lazy optional YuNet (same ONNX the collage worker uses). None if cv2/model missing."""
    if _YUNET["tried"]:
        return _YUNET["det"]
    _YUNET["tried"] = True
    try:
        import cv2
        root = Path(__file__).resolve().parents[1]
        path = next((str(p) for p in (root / "models" / _YUNET_FILE, Path("/app/models") / _YUNET_FILE)
                     if p.is_file() and p.stat().st_size > 100_000), None)
        if path:
            _YUNET["det"] = cv2.FaceDetectorYN.create(path, "", (320, 320), 0.6, 0.3, 5000)
    except Exception:
        pass
    return _YUNET["det"]


def faces(photo: Image.Image):
    """Normalised face boxes [[x0,y0,x1,y1], ...] or [] (soft: never raises)."""
    det = _yunet()
    if det is None:
        return []
    try:
        import cv2
        import numpy as np
        small = photo.copy()
        small.thumbnail((640, 640))
        w, h = small.size
        bgr = cv2.cvtColor(np.array(small.convert("RGB")), cv2.COLOR_RGB2BGR)
        det.setInputSize((w, h))
        _, rows = det.detect(bgr)
        if rows is None or not len(rows):
            return []
        return [[max(x, 0) / w, max(y, 0) / h, min(x + fw, w) / w, min(y + fh, h) / h]
                for x, y, fw, fh in ((float(v) for v in r[:4]) for r in rows) if fw >= 10 and fh >= 10]
    except Exception:
        return []


def _mood(r, g, b, bright, busy):
    warm = r - b
    if bright < 60:
        return "night"
    if warm > 25 and bright > 130:
        return "sunset"
    if warm > 18 and bright >= 90:
        return "golden"
    if g > r and g > b and bright > 70:
        return "green"
    if b > r and bright < 115:
        return "blue_moody"
    if bright > 170 and busy < 0.20:
        return "clean"
    return "everyday"


def analyze(photo: Image.Image) -> dict:
    """Everything the later stages need: mood, stats, 3x3 energy grid, faces."""
    small = photo.convert("RGB")
    small.thumbnail((360, 360))
    stat = ImageStat.Stat(small)
    r, g, b = stat.mean
    bright = (r + g + b) / 3
    edges = small.convert("L").filter(ImageFilter.FIND_EDGES)
    busy = ImageStat.Stat(edges).mean[0] / 255
    w, h = edges.size
    grid = []
    for gy in range(3):
        for gx in range(3):
            cell = edges.crop((gx * w // 3, gy * h // 3, (gx + 1) * w // 3, (gy + 1) * h // 3))
            grid.append(round(ImageStat.Stat(cell).mean[0] / 255, 4))
    return dict(mood=_mood(r, g, b, bright, busy), warm=round((r - b) / 255, 3),
                bright=round(bright / 255, 3), busy=round(busy, 4), grid=grid,
                faces=faces(photo), size=photo.size)


def calm_cell(info: dict, face_penalty=0.35):
    """Index (0..8, row-major) of the calmest grid cell. Cells with faces get penalised."""
    grid = list(info.get("grid") or [0.2] * 9)
    for box in info.get("faces") or []:
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        idx = min(2, int(cy * 3)) * 3 + min(2, int(cx * 3))
        grid[idx] += face_penalty
        for di in (-1, 0, 1):
            j = idx + di
            if 0 <= j < 9:
                grid[j] += face_penalty / 3
    return grid.index(min(grid))
