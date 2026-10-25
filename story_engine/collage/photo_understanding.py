"""Photo feature extraction for the collage worker.

inspect_photo()  -> cheap technical features (sharpness, exposure, contrast, resolution, focus point).
groq_describe()  -> optional vision description. Its quality_score is an LLM guess, so it is returned
                    under `vision_quality`; it never overwrites the measured `technical_quality`.
"""
from PIL import Image, ImageOps, ImageStat, ImageFilter
import io, base64, json, os, requests

_LAPLACIAN = ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128)


def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def _sharpness(gray):
    """Laplacian-style variance on a 512px grayscale copy, mapped to 0..1 (blurry -> 0)."""
    g = gray.copy(); g.thumbnail((512, 512))
    lap = g.filter(_LAPLACIAN)
    var = ImageStat.Stat(lap).var[0]
    return _clamp(var / 600.0)


def find_focus(im):
    """Return (fx, fy, kind) in 0..1 image coordinates: where the crop should keep the subject.
    1) OpenCV Haar face detection if cv2 exists (rembg installs it in the collage image)
    2) edge-energy saliency (PIL only)  3) upper-third bias (faces are usually in the top half)."""
    small = im.copy(); small.thumbnail((480, 480))
    w, h = small.size
    try:
        import cv2, numpy as np
        gray = cv2.cvtColor(np.array(small.convert("RGB")), cv2.COLOR_RGB2GRAY)
        casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        faces = casc.detectMultiScale(gray, 1.15, 5, minSize=(max(24, w // 12), max(24, w // 12)))
        if len(faces):
            fx = sum((x + fw / 2) * fw * fh for x, y, fw, fh in faces) / sum(fw * fh for _, _, fw, fh in faces)
            fy = sum((y + fh / 2) * fw * fh for x, y, fw, fh in faces) / sum(fw * fh for _, _, fw, fh in faces)
            return round(fx / w, 3), round(fy / h, 3), "face"
    except Exception:
        pass
    try:
        edges = small.convert("L").filter(ImageFilter.FIND_EDGES)
        gw, gh = 12, 12
        grid = edges.resize((gw, gh), Image.Resampling.BOX)
        px = list(grid.tobytes())
        tot = sum(px)
        if tot > 0:
            cx = sum((i % gw + .5) * v for i, v in enumerate(px)) / tot / gw
            cy = sum((i // gw + .5) * v for i, v in enumerate(px)) / tot / gh
            return round(cx, 3), round(min(cy, 0.55), 3), "saliency"   # never pull the crop below mid-frame
    except Exception:
        pass
    return 0.5, 0.35, "default"


def ahash(im):
    g = im.convert("L").resize((8, 8), Image.Resampling.BOX)
    px = list(g.tobytes()); avg = sum(px) / 64
    return sum(1 << i for i, v in enumerate(px) if v >= avg)


def inspect_photo(raw):
    full = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    width, height = full.size
    im = full.copy(); im.thumbnail((512, 512)); del full
    stat = ImageStat.Stat(im)
    brightness = sum(stat.mean) / 3
    contrast = sum(stat.stddev) / 3
    sharp = _sharpness(im.convert("L"))
    exposure = _clamp(1.0 - abs(brightness - 125) / 125.0)            # too dark / blown out -> low
    contrast_s = _clamp(contrast / 60.0)
    res = _clamp((width * height) / 1_500_000)
    portrait = 1.0 if height >= width * 0.9 else 0.75                   # stories are vertical
    technical = 0.40 * sharp + 0.25 * exposure + 0.15 * contrast_s + 0.10 * res + 0.10 * portrait
    fx, fy, kind = find_focus(im)
    return {"width": width, "height": height, "brightness": round(brightness, 1),
            "contrast": round(contrast, 1), "sharpness": round(sharp, 3),
            "technical_quality": round(_clamp(technical), 3),
            "dominant_rgb": list(map(int, im.resize((1, 1)).getpixel((0, 0)))),
            "focus": [fx, fy], "focus_kind": kind, "hash": ahash(im), "visual": "photo"}


def groq_describe(raw):
    key = os.getenv("GROQ_API_KEY_COLLAGE") or os.getenv("GROQ_API_KEY_3")
    model = os.getenv("COLLAGE_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
    if not key:
        return {}
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB"); im.thumbnail((768, 768))
    b = io.BytesIO(); im.save(b, "JPEG", quality=78)
    prompt = ("Describe this photo as compact JSON: subject, mood, lighting, colours, location, pose, "
              "quality_score(0-1), category (fashion/kpop/friends/festival/nature/cricket/other). "
              "Do not identify people. JSON only.")
    r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                      headers={"Authorization": "Bearer " + key}, timeout=(5, 15),
                      json={"model": model, "temperature": 0.2, "max_tokens": 220,
                            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()}}]}]})
    r.raise_for_status()
    txt = r.json()["choices"][0]["message"]["content"]
    a, z = txt.find("{"), txt.rfind("}")
    d = json.loads(txt[a:z + 1]) if a >= 0 else {}
    try:
        d["vision_quality"] = _clamp(float(d.pop("quality_score")))
    except Exception:
        d.pop("quality_score", None)
    return d
