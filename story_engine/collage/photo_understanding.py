"""Photo feature extraction for the collage worker.

inspect_photo()  -> cheap technical features (sharpness, exposure, contrast, resolution, face quality,
                    aHash + dHash + colour layout for duplicate / variety checks).
locate_subject() -> subject_box / face_box (YuNet -> rembg mask -> saliency -> default). The selector runs it
                    only for the photos it actually picked.
groq_describe()  -> optional vision description. Its quality_score is an LLM guess, so it is returned
                    under `vision_quality`; it never overwrites the measured `technical_quality`.
"""
from PIL import Image, ImageOps, ImageStat, ImageFilter
import io, base64, json, os, pathlib, threading, logging, requests
from .captions import clean_caption

log = logging.getLogger(__name__)

_LAPLACIAN = ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128)


def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def _sharpness(gray):
    """Laplacian-style variance on a 512px grayscale copy, mapped to 0..1 (blurry -> 0)."""
    g = gray.copy(); g.thumbnail((512, 512))
    lap = g.filter(_LAPLACIAN)
    var = ImageStat.Stat(lap).var[0]
    return _clamp(var / 600.0)


# ----------------------------------------------------------------------------------------------
# Subject location. Result of locate_subject() / find_subject():
#   subject_box : [x0, y0, x1, y1] in 0..1 image coordinates - the region the crop must keep
#   face_box    : union of all detected faces, or None
#   kind        : "face" | "mask" | "saliency" | "default"
#   hard        : True when the box is a real detection (face / rembg mask) -> the crop is validated against it
#                 and falls back to "contain" if it cannot fit. saliency / default are only hints (soft).
# Fallback chain: YuNet face -> rembg mask bounding box -> edge saliency -> default.
# ----------------------------------------------------------------------------------------------
YUNET_FILE = "face_detection_yunet_2023mar.onnx"
YUNET_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/" + YUNET_FILE
_YUNET = {"det": None, "tried": False}
_YUNET_DL = {"tried": False}          # one auto-download attempt per process (never blocks later retries by hand)
_YUNET_LOCK = threading.Lock()
_MIN_FACE_RATIO = float(os.getenv("COLLAGE_MIN_FACE_RATIO", "0.05"))   # drop faces < 5 % of the biggest face's area
_FACE_SCORE = float(os.getenv("COLLAGE_FACE_SCORE", "0.6"))
_DEFAULT_BOX = [0.15, 0.05, 0.85, 0.65]


def _yunet_download(dest):
    """Best-effort one-time fetch of the YuNet ONNX (~230 KB) from the official OpenCV Zoo. Never raises.
    Without this file face detection silently turns off and crops can cut faces, so the worker self-heals."""
    if _YUNET_DL["tried"]:
        return False                                            # already tried this process (maybe succeeded)
    _YUNET_DL["tried"] = True
    if os.getenv("COLLAGE_YUNET_AUTO_DOWNLOAD", "1").lower() in ("0", "false", "off", "no"):
        return False
    try:
        import requests                                         # already a worker dependency (Groq vision)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        tmp = dest + ".part"
        with requests.get(YUNET_URL, timeout=(5, 30), stream=True) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(64 * 1024):
                    f.write(chunk)
        if os.path.getsize(tmp) > 100_000:                      # sanity: real model, not an HTML error page
            os.replace(tmp, dest)
            log.info("collage: YuNet model downloaded to %s", dest)
            return True
        os.remove(tmp)
        log.warning("collage: YuNet download too small; discarding (set COLLAGE_YUNET_MODEL)")
    except Exception as e:
        log.warning("collage: YuNet auto-download failed (%s); set COLLAGE_YUNET_MODEL", e)
    return False


def _yunet_path():
    env = os.getenv("COLLAGE_YUNET_MODEL")
    cands = [env] if env else []
    root = pathlib.Path(__file__).resolve().parents[2]
    cands += [str(root / "models" / YUNET_FILE), "/app/models/" + YUNET_FILE]
    for c in cands:
        if c and os.path.isfile(c) and os.path.getsize(c) > 100_000:
            return c
    for c in cands:                                             # model not shipped: fetch it once (writable dir wins)
        if c and _yunet_download(c) and os.path.isfile(c) and os.path.getsize(c) > 100_000:
            return c
    return None


def _yunet():
    """Lazy singleton cv2.FaceDetectorYN (~230 KB ONNX, a few MB of RAM). None if cv2 / model is missing."""
    if _YUNET["tried"]:
        return _YUNET["det"]
    with _YUNET_LOCK:
        if _YUNET["tried"]:
            return _YUNET["det"]
        _YUNET["tried"] = True
        try:
            import cv2
            path = _yunet_path()
            if path is None:
                log.warning("collage: YuNet model not found even after auto-download (set COLLAGE_YUNET_MODEL); "
                        "face detection OFF -> crops may cut faces")
            else:
                _YUNET["det"] = cv2.FaceDetectorYN.create(path, "", (320, 320), _FACE_SCORE, 0.3, 5000)
        except Exception as e:
            log.warning("collage: YuNet unavailable (%s); using rembg/saliency fallback", e)
        return _YUNET["det"]


def _box_union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _norm_box(b, nd=4):
    x0, y0, x1, y1 = (_clamp(float(v)) for v in b)
    return [round(x0, nd), round(y0, nd), round(max(x1, x0 + 0.01), nd), round(max(y1, y0 + 0.01), nd)]


def detect_faces(im):
    """YuNet face boxes as normalised [x0, y0, x1, y1] lists (YuNet also finds side / small faces). [] if none."""
    det = _yunet()
    if det is None:
        return []
    try:
        import cv2, numpy as np
        small = im.copy(); small.thumbnail((800, 800))
        w, h = small.size
        bgr = cv2.cvtColor(np.array(small.convert("RGB")), cv2.COLOR_RGB2BGR)
        with _YUNET_LOCK:                                        # the detector keeps its input size as state
            det.setInputSize((w, h))
            _, rows = det.detect(bgr)
        if rows is None or not len(rows):
            return []
        faces = []
        for r in rows:
            x, y, fw, fh = (float(v) for v in r[:4])
            if fw < 12 or fh < 12:
                continue
            faces.append([max(x, 0) / w, max(y, 0) / h, min(x + fw, w) / w, min(y + fh, h) / h])
        if not faces:
            return []
        area = lambda b: (b[2] - b[0]) * (b[3] - b[1])
        biggest = max(area(b) for b in faces)
        return [_norm_box(b) for b in faces if area(b) >= _MIN_FACE_RATIO * biggest]
    except Exception:
        log.info("collage: face detection failed", exc_info=True)
        return []


def _mask_box(im):
    """Bounding box (normalised) of the rembg subject mask: whole person / object, even without a visible face."""
    try:
        from . import budget
        if not budget.have(45):
            return None
        from . import collage_renderer as cr
        sess = cr.rembg_session()
        if sess is None:
            return None
        from rembg import remove
        small = im.copy(); small.thumbnail((384, 384))
        raw = io.BytesIO(); small.save(raw, "PNG")
        alpha = Image.open(io.BytesIO(remove(raw.getvalue(), session=sess))).convert("RGBA").getchannel("A")
        hard = alpha.point(lambda a: 255 if a > 100 else 0)
        w, h = hard.size
        px = hard.tobytes()
        cols, rows = [0] * w, [0] * h
        for i, v in enumerate(px):
            if v:
                cols[i % w] += 1; rows[i // w] += 1
        tot = sum(cols)
        if tot < 0.06 * w * h * 1.0:                              # < 6 % of the frame: no real subject
            return None
        def span(arr, n, trim=0.005):                              # ignore stray islands: 0.5 % of mass at each end
            acc, lo, hi = 0, 0, n - 1
            for i, v in enumerate(arr):
                acc += v
                if acc >= trim * tot: lo = i; break
            acc = 0
            for i in range(n - 1, -1, -1):
                acc += arr[i]
                if acc >= trim * tot: hi = i; break
            return lo, hi + 1
        x0, x1 = span(cols, w); y0, y1 = span(rows, h)
        box = [x0 / w, y0 / h, x1 / w, y1 / h]
        if (box[2] - box[0]) * (box[3] - box[1]) > 0.92:         # subject == whole frame: tells us nothing
            return None
        return _norm_box(box)
    except Exception:
        log.info("collage: mask box failed", exc_info=True)
        return None


def _saliency_box(im):
    """Edge-energy box (PIL only): the area holding the middle ~84 % of the detail in x and in y."""
    try:
        small = im.copy(); small.thumbnail((480, 480))
        g = 16
        grid = small.convert("L").filter(ImageFilter.FIND_EDGES).resize((g, g), Image.Resampling.BOX)
        px = list(grid.tobytes())
        if sum(px) <= 0:
            return None
        colm = [sum(px[r * g + c] for r in range(g)) for c in range(g)]
        rowm = [sum(px[r * g + c] for c in range(g)) for r in range(g)]
        def span(arr, q=0.08):
            t = sum(arr); acc = 0; lo = 0; hi = g - 1
            for i, v in enumerate(arr):
                acc += v
                if acc >= q * t: lo = i; break
            acc = 0
            for i in range(g - 1, -1, -1):
                acc += arr[i]
                if acc >= q * t: hi = i; break
            return lo / g, (hi + 1) / g
        x0, x1 = span(colm); y0, y1 = span(rowm)
        if x1 - x0 < 0.25: c = (x0 + x1) / 2; x0, x1 = c - 0.125, c + 0.125
        if y1 - y0 < 0.25: c = (y0 + y1) / 2; y0, y1 = c - 0.125, c + 0.125
        return _norm_box([x0, y0, x1, y1])
    except Exception:
        return None


def person_box_from_face(face):
    """Face box -> preferred 'head and shoulders' box (soft). The face itself stays the hard requirement."""
    fw, fh = face[2] - face[0], face[3] - face[1]
    return _norm_box([face[0] - 0.45 * fw, face[1] - 0.30 * fh, face[2] + 0.45 * fw, face[3] + 1.0 * fh])


def find_subject(im, use_mask=True):
    """Chain: YuNet face -> rembg mask bbox -> saliency -> default. Returns the dict described above."""
    faces = detect_faces(im)
    if faces:
        fb = _box_union(faces)
        return {"subject_box": person_box_from_face(fb), "face_box": _norm_box(fb), "faces": len(faces),
                "kind": "face", "hard": True}
    if use_mask:
        mb = _mask_box(im)
        if mb:
            return {"subject_box": mb, "face_box": None, "faces": 0, "kind": "mask", "hard": True}
    sb = _saliency_box(im)
    if sb:
        return {"subject_box": sb, "face_box": None, "faces": 0, "kind": "saliency", "hard": False}
    return {"subject_box": list(_DEFAULT_BOX), "face_box": None, "faces": 0, "kind": "default", "hard": False}


def locate_subject(raw):
    """bytes -> find_subject() dict. Run only for the photos that were chosen (rembg is the slow step)."""
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    im.thumbnail((1000, 1000))
    return find_subject(im)


def ahash(im):
    g = im.convert("L").resize((8, 8), Image.Resampling.BOX)
    px = list(g.tobytes()); avg = sum(px) / 64
    return sum(1 << i for i, v in enumerate(px) if v >= avg)


def dhash(im):
    """64-bit gradient hash (9x8). Structure-based, so it survives exposure / colour edits better than aHash."""
    px = list(im.convert("L").resize((9, 8), Image.Resampling.BOX).tobytes())
    bits = 0
    for r in range(8):
        for c in range(8):
            if px[r * 9 + c] > px[r * 9 + c + 1]:
                bits |= 1 << (r * 8 + c)
    return bits


def color_sig(im):
    """4x4 RGB layout (48 ints): which colours sit where. Same place / same backdrop -> similar signature."""
    return list(im.resize((4, 4), Image.Resampling.BOX).tobytes())


def _face_quality(det, faces, global_sharp):
    """0..1 quality of the MAIN face (sharpness of the face crop, face exposure, face size). None when no face.
    Picks 'sharp background + blurry face' apart from a truly good portrait."""
    if not faces:
        return None
    area = lambda b: (b[2] - b[0]) * (b[3] - b[1])
    fb = max(faces, key=area)
    w, h = det.size
    x0, y0 = int(fb[0] * w), int(fb[1] * h)
    x1, y1 = max(int(fb[2] * w), x0 + 8), max(int(fb[3] * h), y0 + 8)
    crop = det.crop((x0, y0, x1, y1))
    gray = crop.convert("L")
    sharp = _sharpness(gray) if min(crop.size) >= 24 else global_sharp      # tiny crop: Laplacian is unreliable
    exposure = _clamp(1.0 - abs(ImageStat.Stat(gray).mean[0] - 130) / 130.0)
    size = _clamp((area(fb) ** 0.5) / 0.25)
    q = 0.5 * sharp + 0.25 * exposure + 0.25 * size
    if fb[0] <= 0.002 or fb[1] <= 0.002 or fb[2] >= 0.998 or fb[3] >= 0.998:   # face cut by the frame edge
        q *= 0.8
    return round(_clamp(q), 3)


def inspect_photo(raw):
    full = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    width, height = full.size
    im = full.copy(); im.thumbnail((512, 512))
    det = full.copy(); det.thumbnail((800, 800)); del full          # 800 px copy only for face detection
    stat = ImageStat.Stat(im)
    brightness = sum(stat.mean) / 3
    contrast = sum(stat.stddev) / 3
    sharp = _sharpness(im.convert("L"))
    exposure = _clamp(1.0 - abs(brightness - 125) / 125.0)            # too dark / blown out -> low
    contrast_s = _clamp(contrast / 60.0)
    res = _clamp((width * height) / 1_500_000)
    portrait = 1.0 if height >= width * 0.9 else 0.75                   # stories are vertical
    technical = 0.40 * sharp + 0.25 * exposure + 0.15 * contrast_s + 0.10 * res + 0.10 * portrait
    faces = detect_faces(det)                                           # [] when YuNet / model is missing -> no change
    fq = _face_quality(det, faces, sharp)
    if fq is not None:                                                  # people photos: the FACE must be good too
        technical = 0.75 * technical + 0.25 * fq
    fb = _box_union(faces) if faces else None
    del det
    return {"width": width, "height": height, "brightness": round(brightness, 1),
            "contrast": round(contrast, 1), "sharpness": round(sharp, 3),
            "technical_quality": round(_clamp(technical), 3),
            "faces": len(faces), "face_quality": fq,
            "face_area": round((fb[2] - fb[0]) * (fb[3] - fb[1]), 4) if fb else 0.0,
            "dominant_rgb": list(map(int, im.resize((1, 1)).getpixel((0, 0)))),
            "hash": ahash(im), "dhash": dhash(im), "color_sig": color_sig(im), "visual": "photo"}


def _groq_vision(raw, prompt, max_tokens, size=768):
    """One Groq vision call -> raw model text. Raises on HTTP errors; returns None when no key is configured."""
    key = os.getenv("GROQ_API_KEY_COLLAGE") or os.getenv("GROQ_API_KEY_3")
    model = os.getenv("COLLAGE_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
    if not key:
        return None
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB"); im.thumbnail((size, size))
    b = io.BytesIO(); im.save(b, "JPEG", quality=78)
    r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                      headers={"Authorization": "Bearer " + key}, timeout=(5, 15),
                      json={"model": model, "temperature": 0.2 if max_tokens > 100 else 0.6, "max_tokens": max_tokens,
                            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()}}]}]})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


_CAPTION_RULE = ("caption: 2-3 words, all lowercase, no punctuation, no emoji, no names - a casual label for THIS photo "
                 "(style examples only: \"good light\", \"silk saree\", \"golden hour\"; do not copy them unless they truly fit)")


def groq_describe(raw):
    prompt = ("Describe this photo as compact JSON: subject, mood, lighting, colours, location, pose, "
              "quality_score(0-1), category (fashion/kpop/friends/festival/nature/cricket/other), " + _CAPTION_RULE + ". "
              "Do not identify people. JSON only.")
    txt = _groq_vision(raw, prompt, 260)
    if not txt:
        return {}
    a, z = txt.find("{"), txt.rfind("}")
    d = json.loads(txt[a:z + 1]) if a >= 0 else {}
    try:
        d["vision_quality"] = _clamp(float(d.pop("quality_score")))
    except Exception:
        d.pop("quality_score", None)
    d["caption"] = clean_caption(d.get("caption"))             # "" when unusable -> the renderer uses a neutral fallback
    return d


def groq_caption(raw):
    """Caption-only vision call (tiny prompt) for photos that were picked without a full description."""
    prompt = ("Give a casual 2-3 word lowercase caption for this photo, e.g. \"good light\", \"silk saree\", \"golden hour\" "
              "(examples only, describe THIS photo). No punctuation, no emoji, no names. Reply with the caption only.")
    txt = _groq_vision(raw, prompt, 20, size=512)
    return clean_caption((txt or "").strip().splitlines()[0] if txt and txt.strip() else "")
