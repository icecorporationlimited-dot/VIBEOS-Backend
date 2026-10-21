"""vision_onnx.py - small ONNX models for the photo worker (faces + subject mask).

  detect_faces(img)          -> [(x, y, w, h), ...]  normalised 0..1, top-left
  segment_subject(img, size) -> float32 mask (h, w) 0..1, or None

Design rules (512 MB workers):
  * onnxruntime is imported lazily; every failure returns [] / None so the
    editor falls back to its old heuristics. Nothing here may break an edit.
  * 1 thread, no memory arena, models loaded once per process and cached.
  * RSS guard: if the process is already above VIBEOS_VISION_MAX_RSS_MB the
    models are skipped instead of risking an OOM kill.

Models (see tools/fetch_models.py), both under ./models or $VIBEOS_MODELS_DIR:
  face_detection_yunet_2023mar.onnx   YuNet face detector  (~230 KB)
  u2netp.onnx                         U2-Net-small saliency (~4.7 MB)

Env:  VIBEOS_VISION=0 disables everything.  VIBEOS_FACE_MODEL / VIBEOS_SEG_MODEL
      override file names.  VIBEOS_VISION_MAX_RSS_MB (default 380).
"""
import logging
import os
import threading
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

logger = logging.getLogger("vibeos.vision")

MODELS_DIR = Path(os.environ.get("VIBEOS_MODELS_DIR")
                  or Path(__file__).resolve().parent / "models")
FACE_MODEL = os.environ.get("VIBEOS_FACE_MODEL", "face_detection_yunet_2023mar.onnx")
SEG_MODEL = os.environ.get("VIBEOS_SEG_MODEL", "u2netp.onnx")
MAX_RSS_MB = float(os.environ.get("VIBEOS_VISION_MAX_RSS_MB", "380"))

FACE_LONG_SIDE = 320          # detector input (long side) when the model has a dynamic shape
SEG_SIZE = 320                # U2-Net input
_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_STD = np.array([0.229, 0.224, 0.225], np.float32)

_lock = threading.Lock()
_sessions = {}                # "face"/"seg" -> session or None (None = unavailable, do not retry)
_warned = set()


def enabled():
    return os.environ.get("VIBEOS_VISION", "1") != "0"


def _warn_once(key, msg, *args):
    if key not in _warned:
        _warned.add(key)
        logger.warning(msg, *args)


def _rss_mb():
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 0.0                                    # unknown (Windows/mac) -> do not block


def _session(kind):
    """Cached onnxruntime session for 'face' or 'seg'; None when unavailable."""
    if not enabled():
        return None
    if kind in _sessions:
        return _sessions[kind]
    with _lock:
        if kind in _sessions:
            return _sessions[kind]
        sess = None
        path = MODELS_DIR / (FACE_MODEL if kind == "face" else SEG_MODEL)
        try:
            if not path.is_file():
                _warn_once(("missing", kind), "vision: model %s not found, fallback in use", path)
            elif _rss_mb() > MAX_RSS_MB:
                _warn_once(("rss", kind), "vision: RSS %.0f MB > limit, %s skipped", _rss_mb(), kind)
                return None                       # not cached: may fit in a later (fresh) process
            else:
                import onnxruntime as ort
                so = ort.SessionOptions()
                so.intra_op_num_threads = 1
                so.inter_op_num_threads = 1
                so.enable_cpu_mem_arena = False
                so.enable_mem_pattern = False
                so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
                sess = ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"])
        except Exception as exc:
            _warn_once(("load", kind), "vision: cannot load %s model: %r", kind, exc)
            sess = None
        _sessions[kind] = sess
        return sess


def _fixed_hw(sess, default_hw):
    """(H, W) from the model input when both are static ints, else None."""
    try:
        shape = sess.get_inputs()[0].shape
        h, w = shape[-2], shape[-1]
        if isinstance(h, int) and isinstance(w, int) and h > 0 and w > 0:
            return h, w
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Faces (YuNet)
# ---------------------------------------------------------------------------
def _nms(boxes, scores, thr=0.3):
    order = np.argsort(-scores)
    keep = []
    areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    while order.size:
        i = order[0]
        keep.append(int(i))
        if order.size == 1:
            break
        rest = order[1:]
        ix0 = np.maximum(boxes[i, 0], boxes[rest, 0])
        iy0 = np.maximum(boxes[i, 1], boxes[rest, 1])
        ix1 = np.minimum(boxes[i, 2], boxes[rest, 2])
        iy1 = np.minimum(boxes[i, 3], boxes[rest, 3])
        inter = np.clip(ix1 - ix0, 0, None) * np.clip(iy1 - iy0, 0, None)
        iou = inter / (areas[i] + areas[rest] - inter + 1e-9)
        order = rest[iou <= thr]
    return keep


def _decode_yunet(outs, W, H, min_score):
    """outs: {output_name: array}. -> (boxes[x1,y1,x2,y2] in input px, scores)."""
    boxes, scores = [], []
    for s in (8, 16, 32):
        cls, obj, box = outs.get(f"cls_{s}"), outs.get(f"obj_{s}"), outs.get(f"bbox_{s}")
        if cls is None or obj is None or box is None:
            continue
        cols, rows = W // s, H // s
        cls = np.asarray(cls, np.float32).reshape(-1)
        obj = np.asarray(obj, np.float32).reshape(-1)
        box = np.asarray(box, np.float32).reshape(-1, 4)
        if cls.size != rows * cols or box.shape[0] != cls.size:
            continue
        score = np.sqrt(np.clip(cls, 0, 1) * np.clip(obj, 0, 1))
        idx = np.nonzero(score >= min_score)[0]
        if idx.size == 0:
            continue
        r, c = idx // cols, idx % cols
        b = box[idx]
        cx, cy = (c + b[:, 0]) * s, (r + b[:, 1]) * s
        bw, bh = np.exp(np.clip(b[:, 2], -8, 8)) * s, np.exp(np.clip(b[:, 3], -8, 8)) * s
        boxes.append(np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], 1))
        scores.append(score[idx])
    if not boxes:
        return np.zeros((0, 4), np.float32), np.zeros((0,), np.float32)
    return np.concatenate(boxes), np.concatenate(scores)


def detect_faces(img, min_score=0.6, max_faces=12):
    """PIL image -> [(x, y, w, h)] normalised to the image, top-left origin. [] if unavailable."""
    sess = _session("face")
    if sess is None or _rss_mb() > MAX_RSS_MB:
        return []
    try:
        w, h = img.size
        fixed = _fixed_hw(sess, None)
        if fixed:
            H, W = fixed
            scale = min(W / w, H / h)
        else:
            scale = FACE_LONG_SIDE / max(w, h)
            W = max(32, int(np.ceil(w * scale / 32.0)) * 32)
            H = max(32, int(np.ceil(h * scale / 32.0)) * 32)
        rw, rh = max(1, min(W, int(round(w * scale)))), max(1, min(H, int(round(h * scale))))
        small = img.convert("RGB").resize((rw, rh), Image.Resampling.BILINEAR)
        canvas = np.zeros((H, W, 3), np.float32)
        canvas[:rh, :rw] = np.asarray(small, np.float32)[..., ::-1]        # RGB -> BGR, 0..255
        x = np.ascontiguousarray(canvas.transpose(2, 0, 1)[None])
        names = [o.name for o in sess.get_outputs()]
        res = sess.run(None, {sess.get_inputs()[0].name: x})
        boxes, scores = _decode_yunet(dict(zip(names, res)), W, H, min_score)
        if len(scores) == 0:
            return []
        out = []
        for i in _nms(boxes, scores, 0.3)[:max_faces]:
            x1, y1, x2, y2 = boxes[i] / scale
            x1, y1 = max(0.0, x1 / w), max(0.0, y1 / h)
            x2, y2 = min(1.0, x2 / w), min(1.0, y2 / h)
            if x2 - x1 > 0.015 and y2 - y1 > 0.015:
                out.append((float(x1), float(y1), float(x2 - x1), float(y2 - y1)))
        return out
    except Exception as exc:
        _warn_once("face_run", "vision: face detection failed: %r", exc)
        return []


# ---------------------------------------------------------------------------
# Subject mask (U2-Net-small)
# ---------------------------------------------------------------------------
def _finish_mask(m, size):
    """Normalised 0..1 map (small) -> float32 mask at size=(w, h); None if useless."""
    m = np.clip((m - 0.30) / 0.40, 0.0, 1.0)
    m = m * m * (3 - 2 * m)                                   # smoothstep
    cover = float((m > 0.5).mean())
    if cover < 0.03 or cover > 0.92:                          # nothing found / everything found
        return None
    im = Image.fromarray((m * 255).astype(np.uint8)).resize(size, Image.Resampling.BILINEAR)
    im = im.filter(ImageFilter.GaussianBlur(max(1.0, 0.004 * max(size))))
    return np.asarray(im, np.float32) / 255.0


def segment_subject(img, size=None):
    """PIL image -> float32 (h, w) 'where the subject is', or None if unavailable/useless."""
    sess = _session("seg")
    if sess is None or _rss_mb() > MAX_RSS_MB:
        return None
    try:
        fixed = _fixed_hw(sess, None)
        sh, sw = fixed if fixed else (SEG_SIZE, SEG_SIZE)
        small = img.convert("RGB").resize((sw, sh), Image.Resampling.BILINEAR)
        a = np.asarray(small, np.float32)
        a /= max(float(a.max()), 1e-6)
        a = (a - _MEAN) / _STD
        x = np.ascontiguousarray(a.transpose(2, 0, 1)[None], dtype=np.float32)
        out = np.asarray(sess.run(None, {sess.get_inputs()[0].name: x})[0], np.float32)
        m = out.reshape(out.shape[-2], out.shape[-1])
        lo, hi = float(m.min()), float(m.max())
        if hi - lo < 1e-6:
            return None
        return _finish_mask((m - lo) / (hi - lo), size or img.size)
    except Exception as exc:
        _warn_once("seg_run", "vision: segmentation failed: %r", exc)
        return None


def status():
    """For logs / health checks: which models are usable right now."""
    return {"enabled": enabled(),
            "face": (MODELS_DIR / FACE_MODEL).is_file(),
            "seg": (MODELS_DIR / SEG_MODEL).is_file(),
            "rss_mb": round(_rss_mb(), 1)}
