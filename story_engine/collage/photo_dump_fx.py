"""photo_dump only: grid layout, cream polaroid cards, dust / scratch overlay, face-safe tape, notebook page.

Nothing here is used by the other five styles (collage_styles.photo_dump is the only caller).

Layout   rows by photo count, every card the SAME size, last row centred:
             n=3 2-1 | 4 2-2 | 5 3-2 or 2-3 | 6 3-3 | 7 3-2-2 | 8 3-3-2 | 9 3-3-3
         (n <= 4 uses 2 columns so the cards do not get tiny; n >= 5 uses 3 columns.)
Card     cream paper frame (thin sides, thicker bottom strip for the text), square photo window, faint dust + scratches
         on the photo only.
Tape     translucent, textured, 2 sizes. Never touches the face box: top-centre only when the face does not start in
         the top 20 % of the photo, otherwise on a top corner, otherwise no tape on that card.
Text     one text per card, caption and date alternate (even card = caption, odd card = date). Every text is drawn
         with a baseline anchor at ONE y per row, so caption and date sit on the same baseline.
Page     ruled lines (darker), red margin line, 3 punched holes, light tea stain, soft vignette, ragged page edge and a
         dog-ear on the bottom-right corner, lying on a dark desk.
"""
import math

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

from . import collage_fx as fx
from .collage_layout_planner import GAP, H, SAFE_Y0, SAFE_Y1, W, orientation, photo_size

LANCZOS, BICUBIC = Image.Resampling.LANCZOS, Image.Resampling.BICUBIC

MARGIN_L, MARGIN_R = 92, 56                 # binding side (holes + margin line) is a little wider
CREAM = (252, 245, 224)                     # card frame paper
INK = (40, 34, 28)
DESK = (66, 58, 52)                         # what the page lies on (visible only round the ragged edge / holes)
PAGE = (238, 230, 208)
PAGE_INSET = 14
TAPE_TINTS = ((236, 222, 172), (242, 200, 206), (186, 210, 232), (244, 228, 150), (208, 226, 196))
HAND_SCALE = {"caveat": 1.0, "reenie": 1.3, "gaegu": 0.92}      # Reenie Beanie runs small, Gaegu runs big


# ------------------------------------------------------------------ handwriting
def hand_font(size, weight=600):
    """Handwriting font: Caveat -> Reenie Beanie -> Gaegu. If none was downloaded (tools/fetch_collage_fonts.py) the
    normal family_font fallback applies (Lora italic), so a render never fails."""
    for key in ("caveat", "reenie", "gaegu"):
        if fx.has_font(key):
            return fx.family_font(key, max(10, int(size * HAND_SCALE[key])), weight)
    return fx.family_font("caveat", int(size), weight)


def fit_hand(draw, text, size, max_w, min_size=22, weight=600):
    while size > min_size:
        f = hand_font(size, weight)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 2
    return hand_font(min_size, weight)


# ------------------------------------------------------------------ grid
def row_split(n, rng):
    if n == 5:                                                   # 3+2 or 2+3 (never 2-1-2)
        return (3, 2) if rng.random() < 0.5 else (2, 3)
    fixed = {1: (1,), 2: (2,), 3: (2, 1), 4: (2, 2), 6: (3, 3), 7: (3, 2, 2), 8: (3, 3, 2), 9: (3, 3, 3)}
    if n in fixed:
        return fixed[n]
    rows = [3] * (n // 3)
    if n % 3:
        rows.append(n % 3)
    return tuple(rows)


def build_grid(n, rng, caption=130, gap=GAP):
    """Same-size cards. Returns {rows, cards:[{x,y,w,h,b,photo,strip,row}], cw, ch, b, photo, strip, gap, area}."""
    rows = row_split(n, rng)
    ncols = max(rows)
    aw = W - MARGIN_L - MARGIN_R
    top, bottom = SAFE_Y0 + 24, SAFE_Y1 - caption - gap          # tape may stick out ~18 px above row 1, still >= 250
    avail = bottom - top
    cw = min((aw - gap * (ncols - 1)) // ncols, 700)

    def dims(c):
        b = max(8, round(c * 0.04)); ph = c - 2 * b; strip = round(c * 0.215)
        return b, ph, strip, b + ph + strip
    while True:
        b, ph, strip, ch = dims(cw)
        if len(rows) * ch + (len(rows) - 1) * 40 <= avail or cw <= 200:
            break
        cw -= 4
    gap_v = 0
    if len(rows) > 1:
        gap_v = int(min(160, max(40, (avail - len(rows) * ch) / (len(rows) + 1))))
    y = top + (avail - (len(rows) * ch + (len(rows) - 1) * gap_v)) // 2
    cards = []
    for r, cnt in enumerate(rows):
        x0 = MARGIN_L + (aw - (cnt * cw + (cnt - 1) * gap)) // 2
        for c in range(cnt):
            cards.append({"x": x0 + c * (cw + gap), "y": y, "w": cw, "h": ch, "cw": cw, "ch": ch, "b": b, "photo": ph, "strip": strip, "row": r})
        y += ch + gap_v
    return {"rows": rows, "cards": cards, "cw": cw, "ch": ch, "b": b, "photo": ph, "strip": strip, "gap": gap,
            "area": (MARGIN_L, top, W - MARGIN_R, bottom)}


def make_plan(photos, grid, caption=130):
    """Slot plan in the format collage_layout_planner.check_plan() / the validator expect (slot = the square photo window)."""
    slots = []
    for k, (p, c) in enumerate(zip(photos, grid["cards"])):
        pw, ph = photo_size(p); asp = pw / ph
        slots.append({"x": c["x"] + c["b"], "y": c["y"] + c["b"], "w": c["photo"], "h": c["photo"], "ratio_name": "1:1",
                      "ratio": 1.0, "photo_index": k, "orientation": orientation(asp), "photo_aspect": round(asp, 3),
                      "asset_id": p.get("asset_id")})
    return {"photos": list(photos), "slots": slots, "layout": "-".join(str(r) for r in grid["rows"]),
            "safe": (0, SAFE_Y0, W, SAFE_Y1), "area": grid["area"], "header_top": SAFE_Y0,
            "caption_top": SAFE_Y1 - caption, "gap": grid["gap"], "scale": 1.0, "contain_slots": 0}


# ------------------------------------------------------------------ photo tile (+ where the face is)
def avoid_region(subj):
    """Normalised box the tape must never cover: the face box, else a hard (mask) subject box, else nothing."""
    if not subj:
        return None
    if subj.get("face_box"):
        return list(subj["face_box"])
    if subj.get("kind") == "mask" and subj.get("subject_box"):
        return list(subj["subject_box"])
    return None


def region_in_tile(info, subj, img_size, size):
    """avoid_region mapped into the slot tile's pixels (crop mode: through the crop window; contain mode: through the
    centred, shrunk copy). None when there is no face / hard subject."""
    nb = avoid_region(subj)
    if not nb:
        return None
    iw, ih = img_size; w, h = size
    if info.get("mode") == "crop":
        l, t, r, b = info["box"]; sx, sy = w / (r - l), h / (b - t)
        rect = [(nb[0] * iw - l) * sx, (nb[1] * ih - t) * sy, (nb[2] * iw - l) * sx, (nb[3] * ih - t) * sy]
    else:
        pad, edge = 0.06, 5
        inner = (max(8, int(w * (1 - 2 * pad))), max(8, int(h * (1 - 2 * pad))))
        s = min(inner[0] / iw, inner[1] / ih); fw, fh = iw * s, ih * s
        ox, oy = (w - (int(fw) + 2 * edge)) / 2 + edge, (h - (int(fh) + 2 * edge)) / 2 + edge
        rect = [ox + nb[0] * fw, oy + nb[1] * fh, ox + nb[2] * fw, oy + nb[3] * fh]
    return [max(0.0, rect[0]), max(0.0, rect[1]), min(float(w), rect[2]), min(float(h), rect[3])]


def dust_scratch(tile, rng, strength=1.0):
    """Light dust specks, a few hairs and 2-4 faint vertical film scratches ON the photo only. Deliberately subtle."""
    tile = tile.convert("RGB"); w, h = tile.size
    light, dark = Image.new("L", (w, h), 0), Image.new("L", (w, h), 0)
    dl, dd = ImageDraw.Draw(light), ImageDraw.Draw(dark)
    for _ in range(int(w * h / 1400)):                                   # dust
        x, y, r = rng.uniform(0, w), rng.uniform(0, h), rng.choice((0.6, 0.8, 1.0, 1.3, 1.7))
        (dl if rng.random() < 0.72 else dd).ellipse((x - r, y - r, x + r, y + r), fill=rng.randint(90, 200))
    for _ in range(max(2, int(w * h / 22000))):                          # short curly hairs / fibres
        x, y, ang, curve = rng.uniform(0, w), rng.uniform(0, h), rng.uniform(0, 2 * math.pi), rng.uniform(-0.7, 0.7)
        pts = [(x, y)]
        for _i in range(5):
            ang += curve; x += math.cos(ang) * rng.uniform(1.5, 3.5); y += math.sin(ang) * rng.uniform(1.5, 3.5); pts.append((x, y))
        (dl if rng.random() < 0.6 else dd).line(pts, fill=rng.randint(70, 140), width=1)
    for _ in range(rng.randint(2, 4)):                                   # film scratches
        x, y0, ln = rng.uniform(0.05 * w, 0.95 * w), rng.uniform(-0.1 * h, 0.4 * h), rng.uniform(0.35, 0.9) * h
        drift = rng.uniform(-0.04, 0.04) * w
        pts = [(x + drift * i / 12 + rng.uniform(-0.7, 0.7), y0 + ln * i / 12) for i in range(13)]
        dl.line(pts, fill=rng.randint(60, 120), width=1)
    k = 0.5 * strength
    light = light.filter(ImageFilter.GaussianBlur(0.45)).point(lambda v: int(v * k))
    dark = dark.filter(ImageFilter.GaussianBlur(0.45)).point(lambda v: int(v * k * 0.8))
    out = Image.composite(Image.new("RGB", (w, h), (250, 246, 236)), tile, light)
    return Image.composite(Image.new("RGB", (w, h), (26, 22, 18)), out, dark)


def photo_tile(p, size, loader, rng):
    """-> (tile, info, avoid_rect_in_tile). Face-aware crop (same engine as every style), warm fade grade, dust."""
    from .subject_crop import render_slot, subject_of
    im = loader(p); subj = subject_of(p)
    tile, info = render_slot(im, (size, size), subj)
    region = region_in_tile(info, subj, im.size, (size, size))
    return dust_scratch(fx.grade(tile, "photo_dump"), rng), info, region


# ------------------------------------------------------------------ card
def build_card(tile, g, rng, pad):
    """Cream paper frame: thin sides / top, thicker bottom strip. RGBA with `pad` transparent px all round (room for tape)."""
    b, ph, cw, ch = g["b"], g["photo"], g["cw"], g["ch"]
    card = fx.paper_patch((cw, ch), rng, CREAM).convert("RGBA")
    card.paste(tile.convert("RGBA"), (b, b))
    d = ImageDraw.Draw(card)
    d.rectangle((b - 1, b - 1, b + ph, b + ph), outline=(206, 197, 176, 190), width=1)       # print lip round the photo
    d.rectangle((0, 0, cw - 1, ch - 1), outline=(228, 220, 200, 255), width=1)               # paper edge
    layer = Image.new("RGBA", (cw + 2 * pad, ch + 2 * pad), (0, 0, 0, 0))
    layer.paste(card, (pad, pad))
    return layer


# ------------------------------------------------------------------ tape
def tape_sprite(w, h, angle, rng, tint):
    """Translucent masking tape: streaky fibre texture, slightly see-through (alpha ~0.6), brighter adhesive edges,
    zig-zag torn ends, soft sheen."""
    nr = fx.np_rng(rng)
    streak = nr.standard_normal((h, max(4, w // 6))).astype(np.float32)
    streak = (np.asarray(Image.fromarray(np.clip(streak * 40 + 128, 0, 255).astype(np.uint8), "L").resize((w, h), BICUBIC),
                         np.float32) - 128) / 40
    fine = nr.standard_normal((h, w)).astype(np.float32)
    rgb = np.clip(np.array(tint, np.float32)[None, None, :] + (streak * 9 + fine * 3)[..., None], 0, 255)
    xs = np.arange(w, dtype=np.float32)[None, :]
    a = 178 + streak * 14 + fine * 5 + 14 * np.cos((xs / w * 2.2 + rng.random()) * math.pi)
    a[:2, :] += 22; a[-2:, :] += 22                                     # adhesive edge reads a little denser
    ys = np.arange(h, dtype=np.float32); amp = max(2.0, h * 0.14); period = rng.uniform(4.0, 6.0)
    tri_l = np.abs(((ys / period) % 2) - 1) * amp
    tri_r = np.abs((((ys + period * 0.5) / period) % 2) - 1) * amp
    keep = (xs >= tri_l[:, None]) & (xs <= (w - 1 - tri_r)[:, None])
    alpha = Image.fromarray(np.clip(np.where(keep, a, 0), 0, 255).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(0.5))
    im = Image.fromarray(rgb.astype(np.uint8), "RGB").convert("RGBA"); im.putalpha(alpha)
    return im.rotate(angle, expand=True, resample=BICUBIC)


def _hits(sprite, pos, rect, margin):
    """Does any visible tape pixel fall inside `rect` (grown by margin)? Pixel-exact on the sprite's alpha."""
    if rect is None:
        return False
    x0, y0, x1, y1 = rect[0] - margin, rect[1] - margin, rect[2] + margin, rect[3] + margin
    sx0, sy0 = max(x0, pos[0]), max(y0, pos[1])
    sx1, sy1 = min(x1, pos[0] + sprite.width), min(y1, pos[1] + sprite.height)
    if sx1 <= sx0 or sy1 <= sy0:
        return False
    box = (int(sx0 - pos[0]), int(sy0 - pos[1]), int(math.ceil(sx1 - pos[0])), int(math.ceil(sy1 - pos[1])))
    return sprite.getchannel("A").crop(box).point(lambda a: 255 if a > 12 else 0).getbbox() is not None


def add_tape(layer, g, pad, face, rng):
    """Stick one tape strip on the card layer, never on `face` (rect in photo-tile px, or None).
    Order: top-centre first (only if the face does not start in the top 20 % of the photo) or a top corner first, with the
    other as fallback. If nothing is face-free there is no tape. Returns {kind, size, sprite, pos} or None."""
    cw, b, ph = g["cw"], g["b"], g["photo"]
    sizes = {"small": (round(cw * 0.27), round(cw * 0.085)), "large": (round(cw * 0.46), round(cw * 0.12))}
    rect = None if face is None else (face[0] + pad + b, face[1] + pad + b, face[2] + pad + b, face[3] + pad + b)
    margin = max(6, ph * 0.04)
    top_ok = face is None or face[1] >= 0.20 * ph
    corners = ["tl", "tr"]; rng.shuffle(corners)
    order = (["top"] if top_ok and rng.random() < 0.4 else []) + corners + (["top"] if top_ok and rng.random() >= 0.4 else [])
    tint = rng.choice(TAPE_TINTS)
    for kind in order:
        names = ["large", "small"] if kind == "top" else rng.sample(["small", "large"], 2)
        for name in names:
            tw, th = sizes[name]
            if kind == "top":
                sp = tape_sprite(tw, th, rng.uniform(-5, 5), rng, tint)
                cx, cy = pad + cw / 2 + rng.uniform(-0.1, 0.1) * cw, pad + 2
            else:
                ang = rng.uniform(32, 46) * (1 if kind == "tl" else -1)
                sp = tape_sprite(tw, th, ang, rng, tint)
                sx = 1 if kind == "tl" else -1
                cx = (pad if kind == "tl" else pad + cw) + sx * (sp.width / 2 - 12)       # only ~12 px sticks out of the card
                cy = pad + (sp.height / 2 - 12)
            pos = (int(cx - sp.width / 2), int(cy - sp.height / 2))
            if _hits(sp, pos, rect, margin):
                continue
            fx.blit(layer, sp, pos[0], pos[1])
            return {"kind": kind, "size": name, "sprite": sp, "pos": pos}
    return None


# ------------------------------------------------------------------ page
def _aa_poly(size, pts, ss=4):
    """Anti-aliased polygon mask (L) of `size`; pts in that patch's pixels."""
    m = Image.new("L", (size[0] * ss, size[1] * ss), 0)
    ImageDraw.Draw(m).polygon([(x * ss, y * ss) for x, y in pts], fill=255)
    return m.resize(size, LANCZOS)


def _cut(mask, box, patch):
    """Remove `patch` (L, 255 = remove) from `mask` at box=(x, y)."""
    x, y = box
    region = mask.crop((x, y, x + patch.width, y + patch.height))
    mask.paste(ImageChops.darker(region, ImageOps.invert(patch)), (x, y))


def page_paper(rng):
    """Notebook paper: darker blue ruled lines, red margin line, light tea stain, soft vignette (RGB, canvas size)."""
    arr = np.asarray(fx.paper_bg(PAGE, "fine").convert("RGB"), np.float32).copy()

    ink = Image.new("L", (W, H), 0); d = ImageDraw.Draw(ink)
    for y in range(150, H - 60, 54):
        d.line((0, y, W, y), fill=int(rng.uniform(205, 255)), width=2)
    a = (np.asarray(ink.filter(ImageFilter.GaussianBlur(0.5)), np.float32) / 255 * 0.52)[..., None]
    arr = arr * (1 - a) + np.array((92, 124, 166), np.float32) * a                 # ruled lines: a bit darker than before

    mg = Image.new("L", (W, H), 0); ImageDraw.Draw(mg).line((80, 0, 80, H), fill=255, width=2)
    a = (np.asarray(mg.filter(ImageFilter.GaussianBlur(0.5)), np.float32) / 255 * 0.5)[..., None]
    arr = arr * (1 - a) + np.array((200, 84, 92), np.float32) * a                  # margin line

    cx, cy = rng.choice(((870, 1690), (190, 1700), (900, 300), (170, 1010)))      # light stain: a tea ring
    r = rng.uniform(95, 135); pad = int(r * 1.5)
    x0, y0, x1, y1 = max(0, cx - pad), max(0, cy - pad), min(W, cx + pad), min(H, cy + pad)
    yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
    ang = np.arctan2(yy - cy, xx - cx); ph1, ph2 = rng.uniform(0, 6.28), rng.uniform(0, 6.28)
    rr = r * (1 + 0.06 * np.sin(3 * ang + ph1) + 0.04 * np.sin(5 * ang + ph2))
    dist = np.hypot(xx - cx, yy - cy) / rr
    blot = np.asarray(Image.fromarray(np.clip(fx.np_rng(rng).standard_normal((8, 8)) * 40 + 128, 0, 255).astype(np.uint8), "L")
                      .resize((x1 - x0, y1 - y0), BICUBIC), np.float32) / 255
    dens = (0.9 * np.exp(-((dist - 1) / 0.045) ** 2) + 0.22 / (1 + np.exp((dist - 1) * 40))) * (0.65 + 0.7 * blot)
    col = np.array((176, 138, 86), np.float32) / 255
    arr[y0:y1, x0:x1] *= 1 - 0.26 * dens[..., None] * (1 - col)[None, None, :]

    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)                                 # halka vignette (max ~11 % in the corners)
    rad = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 * 0.8 + ((yy - H / 2) / (H / 2)) ** 2 * 0.8)
    v = np.clip((rad - 0.55) / 0.75, 0, 1) ** 2
    arr *= 1 - 0.12 * v[..., None] * np.array((0.8, 1.0, 1.25), np.float32)[None, None, :]
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")


DOG_L = 104
DOG_P = (W - 20, H - 20)                                                          # bottom-right page corner


def page_mask(rng):
    """Ragged page outline + 3 punched holes + the corner that is folded over (dog-ear)."""
    m = Image.new("L", (W, H), 0)
    m.paste(fx.torn_mask((W - 2 * PAGE_INSET, H - 2 * PAGE_INSET), rng, amp=6, wavelength=80, fibre=True), (PAGE_INSET, PAGE_INSET))
    r = 14
    for hy in (int(H * 0.18), int(H * 0.50), int(H * 0.82)):
        x, y = 46 - r - 3, hy - r - 3
        _cut(m, (x, y), _aa_poly((2 * r + 6, 2 * r + 6), [(3 + r + r * math.cos(t * math.pi / 24), 3 + r + r * math.sin(t * math.pi / 24)) for t in range(48)]))
    px, py = DOG_P; L = DOG_L; ox, oy = px - L - 20, py - L - 20
    tri = [(px - L - ox, py - oy), (px + 40 - ox, py + 40 - oy), (px - ox, py - L - oy)]
    _cut(m, (ox, oy), _aa_poly((L + 80, L + 80), tri))
    return m


def add_dog_ear(canvas):
    """Folded corner: soft shadow it throws on the page, then the flap (back of the paper, darker at the crease)."""
    px, py = DOG_P; L = DOG_L
    ox, oy = px - L - 14, py - L - 14; size = (L + 28, L + 28)
    a, bq, tip = (px - L - ox, py - oy), (px - ox, py - L - oy), (px - L - ox, py - L - oy)
    shadow = _aa_poly(size, [(x - 5, y - 5) for x, y in (a, bq, tip)]).filter(ImageFilter.GaussianBlur(6)).point(lambda v: int(v * 0.38))
    region = canvas.crop((ox, oy, ox + size[0], oy + size[1])).convert("RGB")
    canvas.paste(Image.composite(ImageChops.multiply(region, Image.new("RGB", size, (92, 70, 48))), region, shadow).convert("RGBA"), (ox, oy))
    yy, xx = np.mgrid[0:size[1], 0:size[0]].astype(np.float32)
    t = np.clip(((a[0] + a[1]) - (xx + yy)) / L, 0, 1)[..., None]               # 0 at the crease, 1 at the folded tip
    col = np.array((216, 206, 182), np.float32) * (1 - t) + np.array((246, 241, 226), np.float32) * t
    flap = Image.fromarray(col.astype(np.uint8), "RGB").convert("RGBA")
    flap.putalpha(_aa_poly(size, [a, bq, tip]))
    d = ImageDraw.Draw(flap); d.line((a[0], a[1], bq[0], bq[1]), fill=(150, 130, 100, 110), width=2)           # crease
    canvas.alpha_composite(flap, (ox, oy))


def build_page(rng):
    """Whole 1080x1920 background: dark desk, page with ragged edge / holes / dog-ear and its shadow."""
    canvas = fx.paper_bg(DESK, "crumple")
    page = page_paper(rng).convert("RGBA"); page.putalpha(page_mask(rng))
    fx.cast_shadow(canvas, page, 0, 0, mode="normal", contact=(2.5, (1, 2), 0.55), ambient=(14, (3, 5), 0.5))
    canvas.alpha_composite(page)
    add_dog_ear(canvas)
    return canvas
