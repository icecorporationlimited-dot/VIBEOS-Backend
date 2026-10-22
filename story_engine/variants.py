"""story_engine/variants.py — Step 10b: ek hi rendered story se alag-alag crop variants.

Har user ko wahi trend nahi milta (feed.py), par asset ke andar bhi thodi variety chahiye: ek chhota zoom + anchor.
Crop sirf serve karte waqt lagta hai (api_routes), render dobara nahi hota.

Safe limit: MAX_ZOOM 1.04 => zyada se zyada 4% (~43 px chaude / ~77 px lambai) kat'ta hai.
Templates ka text 80 px side / 150 px top / 220 px bottom padding ke andar hai, isliye text kabhi nahi katta.
"""
import hashlib
import io

MAX_ZOOM = 1.04
ANCHORS = ("center", "top", "bottom", "left", "right")
_ZOOMS = (1.0, 1.02, 1.04)


def pick_crop(user_id, asset_id) -> dict:
    """Deterministic: same (user, asset) hamesha same crop; alag user ko alag crop milne ke chance zyada."""
    h = hashlib.sha256(f"{user_id}|{asset_id}".encode()).digest()
    return {"zoom": _ZOOMS[h[0] % len(_ZOOMS)], "anchor": ANCHORS[h[1] % len(ANCHORS)]}


def clean_crop(crop) -> dict:
    """DB se aaya crop bhi clamp karo, taaki galat value text na kaate."""
    crop = crop if isinstance(crop, dict) else {}
    try:
        zoom = float(crop.get("zoom", 1.0))
    except (TypeError, ValueError):
        zoom = 1.0
    anchor = crop.get("anchor") if crop.get("anchor") in ANCHORS else "center"
    return {"zoom": min(max(zoom, 1.0), MAX_ZOOM), "anchor": anchor}


def apply_crop(jpeg: bytes, crop) -> bytes:
    """Zoom + anchor crop, phir original size par wapas. zoom == 1.0 ya Pillow na ho to bytes jaise ke taise."""
    c = clean_crop(crop)
    if c["zoom"] <= 1.0:
        return jpeg
    try:
        from PIL import Image
    except ImportError:
        return jpeg
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    w, h = im.size
    cw, ch = round(w / c["zoom"]), round(h / c["zoom"])
    x = {"left": 0, "right": w - cw}.get(c["anchor"], (w - cw) // 2)
    y = {"top": 0, "bottom": h - ch}.get(c["anchor"], (h - ch) // 2)
    out = io.BytesIO()
    im.crop((x, y, x + cw, y + ch)).resize((w, h), Image.LANCZOS).save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()
