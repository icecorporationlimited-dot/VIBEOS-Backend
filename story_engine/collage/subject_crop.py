"""Subject-aware cropping for collage slots.

Input is a `subject` dict stored by photo_understanding.locate_subject():
    subject_box [x0,y0,x1,y1] (0..1)  region that must survive the crop
    face_box    [x0,y0,x1,y1] | None  union of detected faces (always a HARD requirement)
    kind        face | mask | saliency | default
Only detections are hard: face_box, and subject_box when kind == "mask" (whole person / object).
saliency / default boxes are hints: they steer the window but never force the contain fallback.

Rules (plan_crop):
  * the crop window is the largest window with the slot's aspect ratio that fits the photo (no extra zoom);
    it is only *moved* so the required box stays inside, never made smaller.
  * required box = subject_box + margin; if that cannot fit, shrink the margin, then drop to the bare face box.
  * with a face there is at least HEADROOM (12 % of the window height) above the face, as far as the photo has it.
  * if even the bare required box does not fit the slot's ratio -> mode "contain": whole photo, no zoom,
    over a blurred copy of itself.
  * every crop is validated: face box (and hard subject box) 100 % inside, else "contain".
render_slot() returns (image, info); arrange() reorders photos so as few slots as possible need "contain".
"""
from PIL import Image, ImageOps, ImageFilter, ImageEnhance, ImageDraw
import itertools, logging, math, os

log = logging.getLogger(__name__)
HEADROOM = float(os.getenv("COLLAGE_HEADROOM", "0.12"))        # fraction of crop height kept above the face
_EPS = 1e-6
_SOFT_DEFAULT = {"subject_box": [0.15, 0.05, 0.85, 0.65], "face_box": None, "kind": "default", "hard": False}


def subject_of(p):
    """The subject dict of a photo dict (key `subject_region`; `subject` is the vision caption) or a plain subject dict."""
    if not p:
        return None
    return p.get("subject_region") if "subject_region" in p else (p if "subject_box" in p else None)


def _px(box, iw, ih):
    return [box[0] * iw, box[1] * ih, box[2] * iw, box[3] * ih]


def _window(iw, ih, a):
    """Largest crop window with aspect `a` (= w/h) inside the photo."""
    return (ih * a, ih) if iw / ih > a else (iw, iw / a)


def _levels(subj):
    """[(name, box, margins, hard)] from most to least demanding."""
    sb, fb, kind = subj.get("subject_box"), subj.get("face_box"), subj.get("kind")
    out = []
    if kind == "mask" and sb:
        out.append(("subject", sb, (0.06, 0.02, 0.0), True))
    elif kind == "face" and sb and fb:
        out.append(("person", sb, (0.04, 0.0), False))
    if fb:
        out.append(("face", fb, (0.35, 0.15, 0.0), True))
    if not out and sb:
        out.append(("hint", sb, (), False))
    return out


def _expand(box, m, iw, ih):
    bw, bh = box[2] - box[0], box[3] - box[1]
    return [max(0.0, box[0] - m * bw), max(0.0, box[1] - m * bh), min(float(iw), box[2] + m * bw), min(float(ih), box[3] + m * bh)]


def validate_crop(crop, subj, iw, ih):
    """crop = (l, t, r, b) in source px. Face box (and hard subject box) must be 100 % inside."""
    l, t, r, b = crop

    def inside(box):
        x0, y0, x1, y1 = _px(box, iw, ih)
        return l <= x0 + 0.5 and t <= y0 + 0.5 and r >= x1 - 0.5 and b >= y1 - 0.5
    res = {"face_inside": None, "subject_inside": None}
    if subj.get("face_box"):
        res["face_inside"] = inside(subj["face_box"])
    if subj.get("kind") == "mask" and subj.get("subject_box"):
        res["subject_inside"] = inside(subj["subject_box"])
    res["ok"] = all(v is not False for v in (res["face_inside"], res["subject_inside"]))
    return res


def plan_crop(iw, ih, size, subject):
    """-> {"mode": "crop", "box": (l,t,r,b), "level":..., "valid": {...}} or {"mode": "contain", "reason": ...}."""
    w, h = size
    a = w / h
    subj = subject or _SOFT_DEFAULT
    cw, ch = _window(iw, ih, a)
    face = _px(subj["face_box"], iw, ih) if subj.get("face_box") else None
    head = HEADROOM * ch if face else 0.0
    levels = _levels(subj) or [("hint", _SOFT_DEFAULT["subject_box"], (), False)]
    hard_seen = any(lv[3] for lv in levels)

    chosen = None
    for name, box, margins, hard in levels:
        base = _px(box, iw, ih)
        for m in (margins or (0.0,)):
            req = _expand(base, m, iw, ih)
            if face:                                        # headroom above the face, limited by the photo's top edge
                req[1] = max(0.0, min(req[1], face[1] - head))
            if req[2] - req[0] <= cw + _EPS and req[3] - req[1] <= ch + _EPS:
                chosen = (name, base, req, hard, m); break
        if chosen:
            break
    if chosen is None:
        if hard_seen:
            return {"mode": "contain", "reason": "subject does not fit slot ratio"}
        name, box, _, hard = levels[0]                     # soft hint only: best effort, window just follows it
        base = _px(box, iw, ih)
        cx, cy = (base[0] + base[2]) / 2, (base[1] + base[3]) / 2
        req = [cx, cy, cx, cy]                              # centre-follow, no containment guarantee
        chosen = (name, base, req, False, 0.0)

    name, base, req, hard, m = chosen
    ax = (face[0] + face[2]) / 2 if face else (base[0] + base[2]) / 2
    ay = (face[1] + face[3]) / 2 if face else (base[1] + base[3]) / 2
    frac = 0.40 if face else 0.5                              # face sits a bit above the middle
    lo_x, hi_x = max(0.0, req[2] - cw), min(iw - cw, req[0])
    lo_y, hi_y = max(0.0, req[3] - ch), min(ih - ch, req[1])
    if lo_x > hi_x: lo_x = hi_x = min(max(0.0, (req[0] + req[2]) / 2 - cw / 2), iw - cw)
    if lo_y > hi_y: lo_y = hi_y = min(max(0.0, (req[1] + req[3]) / 2 - ch / 2), ih - ch)
    left = min(max(ax - cw / 2, lo_x), hi_x)
    top = min(max(ay - frac * ch, lo_y), hi_y)
    crop = (max(0, math.floor(left + _EPS)), max(0, math.floor(top + _EPS)),
            min(iw, math.ceil(left + cw - _EPS)), min(ih, math.ceil(top + ch - _EPS)))
    valid = validate_crop(crop, subj, iw, ih)
    if not valid["ok"]:
        return {"mode": "contain", "reason": "validation failed", "valid": valid}
    return {"mode": "crop", "box": crop, "level": name, "margin": m, "valid": valid}


def can_crop(iw, ih, size, subject):
    return plan_crop(iw, ih, size, subject)["mode"] == "crop"


def contain(photo, size, pad=0.06):
    """Whole photo, no zoom, on a blurred + dimmed copy of itself with a thin white edge and soft shadow."""
    w, h = size
    sw, sh = max(8, w // 8), max(8, h // 8)
    bg = ImageOps.fit(photo, (sw, sh), Image.Resampling.BILINEAR, centering=(0.5, 0.4)).filter(ImageFilter.GaussianBlur(3))
    bg = ImageEnhance.Brightness(bg.resize((w, h), Image.Resampling.BICUBIC)).enhance(0.8).convert("RGBA")
    inner = (max(8, int(w * (1 - 2 * pad))), max(8, int(h * (1 - 2 * pad))))
    fg = ImageOps.contain(photo, inner, Image.Resampling.LANCZOS)
    edge = 5
    framed = Image.new("RGBA", (fg.width + 2 * edge, fg.height + 2 * edge), (255, 255, 255, 255))
    framed.paste(fg, (edge, edge))
    x, y = (w - framed.width) // 2, (h - framed.height) // 2
    shadow = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rectangle((x + 3, y + 7, x + framed.width + 3, y + framed.height + 7), fill=(0, 0, 0, 120))
    bg.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(9)))
    bg.alpha_composite(framed, (x, y))
    return bg.convert("RGB")


def render_slot(photo, size, subject=None):
    """-> (RGB image of exactly `size`, info). info["mode"] is "crop" or "contain"."""
    w, h = size
    iw, ih = photo.size
    plan = plan_crop(iw, ih, (w, h), subject)
    if plan["mode"] == "crop":
        img = photo.crop(plan["box"]).resize((w, h), Image.Resampling.LANCZOS)
        return img, plan
    log.info("collage: contain mode for %dx%d slot (%s)", w, h, plan.get("reason"))
    return contain(photo, (w, h)), plan


def arrange(photos, sizes):
    """Reorder `photos` for slots of `sizes` so as few photos as possible need contain mode (then the least
    shuffling away from quality order). Photos need width/height; without them the order is kept."""
    n = len(photos)
    if n < 2 or n != len(sizes) or n > 6 or any(not (p.get("width") and p.get("height")) for p in photos):
        return list(photos)
    ok = [[can_crop(p["width"], p["height"], s, subject_of(p)) for s in sizes] for p in photos]
    best, best_cost = None, None
    for perm in itertools.permutations(range(n)):             # perm[slot] = photo index
        cost = (sum(1 for s, i in enumerate(perm) if not ok[i][s]), sum(abs(s - i) for s, i in enumerate(perm)))
        if best_cost is None or cost < best_cost:
            best, best_cost = perm, cost
    if best_cost and best_cost[0]:
        log.info("collage: %d slot(s) still need contain mode after re-arranging", best_cost[0])
    return [photos[i] for i in best]
