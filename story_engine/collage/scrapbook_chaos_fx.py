"""Scrapbook Chaos = dense, overlapping, no empty background. Used ONLY by collage_styles.scrapbook_chaos
(no other style touches this file).

Look
  * the whole canvas is paper: a cream base, then big torn sheets in kraft / pink / cream (some ruled notebook
    pages) on a jittered grid, then smaller torn scraps and washi tape on top. No flat background anywhere, also not
    in the top / bottom Instagram bands (those hold paper, tape and stickers only - never a photo or the title).
  * photos are NOT rectangles: every photo is cut to an irregular torn / scissor-cut shape (chamfered corners, a notch,
    large fractal wobble + paper fibres) with a ragged white paper rim. A few photos (about 40 %, at least one) are
    instead a rembg cut-out of the subject (die-cut sticker look) on a torn paper mount. rembg missing / failed ->
    that photo just gets the irregular torn shape.
  * photos are scaled ~1.1-1.25x their slot and tilted, so neighbours overlap each other (the planner still uses
    gap 0 + plan["min_gap"] = 0, exactly like playful_hype, because overlap is the point). Every photo stays inside
    the story safe zone (y 250 .. caption_top).
  * title = ransom note: every letter has its own font, its own paper colour and rotation, on its own paper scrap.
  * flowers / stickers are PNGs (assets/collage/stickers/: flower_*.png, leaf_sprig.png + the old doodles), with a
    white die-cut border; some sit under the photos, some over them.
  * the handwritten note (the copy text) lives on a torn notebook scrap with two tapes; two photos get a small
    handwritten tag as well.
  * paper layers use exactly three paper colours: kraft, pink, cream.

Contract with the rest of the pipeline: plan = collage_layout_planner.plan_slots() (slots inside y 250 .. 1670,
fixed ratios, orientation matched); nothing in the validator changes.
"""
import math
import random

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

from . import collage_fx as fx
from .collage_layout_planner import SAFE_Y0, SAFE_Y1, MARGIN_X

W, H = 1080, 1920
BICUBIC, LANCZOS = Image.Resampling.BICUBIC, Image.Resampling.LANCZOS

# the three paper colours (+ the notebook-ruled variant of cream). name -> (tint, texture kind)
PAPER = {"kraft": ((203, 162, 114), "crumple"), "pink": ((241, 178, 196), "fine"),
         "cream": ((250, 242, 222), "fine"), "ruled": ((250, 246, 232), "ruled")}
PAPER_WEIGHTS = (("kraft", .32), ("pink", .30), ("cream", .26), ("ruled", .12))
CAPTION = 330                                  # px reserved under the photos for title + note
TITLE_LETTER_COLOURS = [(231, 140, 120), (118, 90, 150), (240, 196, 70), (86, 130, 110), (176, 80, 90),
                        (120, 150, 200), (240, 170, 190), (60, 60, 66), (250, 244, 228), (203, 162, 114)]
INK = (36, 32, 40)
DOODLE_COLOURS = [(255, 214, 60), (255, 120, 150), (120, 200, 255), (140, 225, 160), (255, 150, 70), (190, 150, 255)]
FLOWERS = ("flower_daisy", "flower_blossom", "flower_blossom_lilac", "flower_sunflower", "flower_cosmos", "flower_tulip")
DOODLES = ("heart", "star", "sparkle", "asterisk", "smiley", "sun", "exclaim", "squiggle", "arrow")
WASHI = (("washi_stripe", (255, 150, 180)), ("washi_dots", (140, 185, 255)), ("washi_stripe", (255, 214, 100)),
         ("masking", (236, 226, 186)), ("washi_dots", (170, 225, 170)), ("clear", None), ("masking", (236, 226, 186)))
SH_SHEET = dict(contact=(2.5, (1, 3), .30), ambient=(14, (4, 8), .20))
SH_SCRAP = dict(contact=(3, (1, 3), .45), ambient=(12, (3, 7), .28))
SH_PHOTO = dict(contact=(3.5, (2, 4), .62), ambient=(22, (8, 17), .38))
SH_TAPE = dict(contact=(2.5, (1, 3), .5), ambient=(7, (2, 5), .22))


# =============================================================== paper
def _weighted(rng, pairs):
    r, acc = rng.random() * sum(w for _, w in pairs), 0
    for k, w in pairs:
        acc += w
        if r <= acc:
            return k
    return pairs[-1][0]


def tex_patch(size, rng, tint, kind="fine"):
    """RGB patch of `size`: `tint` x a random crop of the paper texture (multiply)."""
    tex, mean = fx.paper_texture(kind)
    w, h = size
    cw, ch = min(w, tex.width), min(h, tex.height)
    x, y = rng.randint(0, tex.width - cw), rng.randint(0, tex.height - ch)
    t = tex.crop((x, y, x + cw, y + ch))
    if (cw, ch) != (w, h):
        t = t.resize((w, h), BICUBIC)
    if rng.random() < .5:
        t = ImageOps.mirror(t)
    comp = 255.0 / mean
    base = Image.new("RGB", (w, h), tuple(min(255, int(c * comp)) for c in tint[:3]))
    return ImageChops.multiply(base, t.convert("RGB"))


def sheet(size, rng, kind, amp=9, wavelength=90):
    """Torn sheet of paper (RGBA) in kraft / pink / cream / ruled."""
    tint, tk = PAPER[kind]
    out = tex_patch(size, rng, tint, tk).convert("RGBA")
    out.putalpha(fx.torn_mask(size, rng, amp=amp, wavelength=wavelength, fibre=True))
    return out


def _rot(im, angle):
    return im.rotate(angle, expand=True, resample=BICUBIC) if angle else im


def background(canvas, rng):
    """Cover the whole canvas with overlapping torn paper (kraft / pink / cream), scraps and washi tape."""
    cols, rows = 3, 5
    cw, ch = W / cols, H / rows
    order = [(c, r) for c in range(cols) for r in range(rows)]
    rng.shuffle(order)
    kinds = [_weighted(rng, PAPER_WEIGHTS) for _ in order]
    for need in ("kraft", "pink", "cream"):                       # all three colours are always present
        if need not in kinds:
            kinds[rng.randrange(len(kinds))] = need
    cover = Image.new("L", (W // 6, H // 6), 0)                      # coverage map (1/6 scale): where is paper already?

    def lay(kind, cx, cy, sw, sh):
        sp = _rot(sheet((sw, sh), rng, kind, amp=rng.randint(7, 12)), rng.uniform(-16, 16))
        x, y = int(cx - sp.width / 2), int(cy - sp.height / 2)
        fx.place(canvas, sp, x, y, **SH_SHEET)
        a = sp.getchannel("A").resize((max(1, sp.width // 6), max(1, sp.height // 6)), Image.Resampling.BOX)
        cover.paste(255, (x // 6, y // 6), a.point(lambda v: 255 if v > 200 else 0))
    for (c, r), kind in zip(order, kinds):
        lay(kind, (c + .5) * cw + rng.uniform(-.18, .18) * cw, (r + .5) * ch + rng.uniform(-.18, .18) * ch,
            int(cw * rng.uniform(1.5, 1.95)), int(ch * rng.uniform(1.5, 1.9)))
    for _ in range(14):                                               # plug any hole where the base paper still shows
        hole = ImageChops.invert(cover).filter(ImageFilter.MinFilter(5))
        bb = hole.point(lambda v: 255 if v > 128 else 0).getbbox()
        if not bb:
            break
        lay(_weighted(rng, PAPER_WEIGHTS[:3]), (bb[0] + bb[2]) * 3, (bb[1] + bb[3]) * 3,
            max(360, (bb[2] - bb[0]) * 6 + 260), max(300, (bb[3] - bb[1]) * 6 + 260))
    # smaller scraps: stratified over a 4 x 7 grid so the top / bottom bands and the sides get paper layers too
    cells = [(c, r) for c in range(4) for r in range(7)]
    rng.shuffle(cells)
    for c, r in cells[:15]:
        w_, h_ = rng.randint(250, 470), rng.randint(110, 280)
        tint = PAPER[_weighted(rng, PAPER_WEIGHTS[:3])][0]
        sp = _rot(fx.scrap((w_, h_), rng, tint), rng.uniform(-22, 22))
        cx, cy = (c + .5) * W / 4 + rng.uniform(-70, 70), (r + .5) * H / 7 + rng.uniform(-90, 90)
        fx.place(canvas, sp, int(cx - sp.width / 2), int(cy - sp.height / 2), **SH_SCRAP)
    for _ in range(7):                                              # washi tape across the seams
        kind, tint = rng.choice(WASHI)
        tp = fx.tape_sprite(kind, rng.randint(160, 230), rng.randint(38, 48), rng.uniform(-40, 40), rng, tint)
        fx.place(canvas, tp, rng.randint(-20, W - 120), rng.randint(-10, H - 60), **SH_TAPE)


# =============================================================== irregular photo shapes
def irregular_mask(size, rng, amp=9.0):
    """Mask (L, `size`) that is clearly NOT a rectangle: chamfered corners (random size / slant), sometimes a bite out of an
    edge, then every edge wobbles by fractal noise (+-amp px) and gets paper-fibre fuzz. Built at 2x, LANCZOS down."""
    w, h = size
    m = min(w, h)
    pad = amp + 2

    def cut():
        return rng.uniform(.10, .32) * m
    cuts = [rng.random() < .72 for _ in range(4)]
    while sum(cuts) < 2:
        cuts[rng.randrange(4)] = True
    pts = []                                                        # clockwise from top-left (screen coordinates)
    corners = ((0, 0), (w, 0), (w, h), (0, h))
    for k, ((cx, cy), is_cut) in enumerate(zip(corners, cuts)):
        if not is_cut:
            pts.append((cx, cy)); continue
        a, b = cut(), cut()
        if k == 0:   pts += [(0, b), (a, 0)]
        elif k == 1: pts += [(w - a, 0), (w, b)]
        elif k == 2: pts += [(w, h - a), (w - b, h)]
        else:        pts += [(a, h), (0, h - b)]
    if rng.random() < .65:                                          # a bite / slanted dip in one edge
        i = rng.randrange(len(pts)); p, q = pts[i], pts[(i + 1) % len(pts)]
        t = rng.uniform(.3, .7); mx, my = p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t
        ex, ey = q[0] - p[0], q[1] - p[1]; ln = math.hypot(ex, ey) or 1
        depth = rng.uniform(.03, .09) * m
        pts.insert(i + 1, (mx - ey / ln * depth, my + ex / ln * depth))     # inward normal for a clockwise polygon
    sx, sy = (w - 2 * pad) / w, (h - 2 * pad) / h                  # keep the whole shape (incl. wobble) inside the tile
    pts = [(pad + x * sx, pad + y * sy) for x, y in pts]
    S = fx.SS
    step = 3.0
    dense = []
    for i in range(len(pts)):                                       # densify every edge
        (x0, y0), (x1, y1) = pts[i], pts[(i + 1) % len(pts)]
        n = max(2, int(math.hypot(x1 - x0, y1 - y0) * S / step))
        for j in range(n):
            t = j / n; dense.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    P = np.array(dense, np.float32) * S
    n = len(P)
    nxt, prv = np.roll(P, -1, 0), np.roll(P, 1, 0)
    tang = nxt - prv
    norm = np.stack([tang[:, 1], -tang[:, 0]], 1)                   # outward for a clockwise polygon
    norm /= (np.linalg.norm(norm, axis=1, keepdims=True) + 1e-6)
    k = np.ones(17, np.float32) / 17                                # smooth the normals round the corners
    for c in range(2):
        norm[:, c] = np.convolve(np.concatenate([norm[-8:, c], norm[:, c], norm[:8, c]]), k, "valid")[:n]
    norm /= (np.linalg.norm(norm, axis=1, keepdims=True) + 1e-6)
    d = np.clip(fx._periodic_fbm(n, rng, max(8, int(rng.uniform(90, 170) * S / step)), 3) * 1.9, -1, 1) * amp * S
    P = P + norm * d[:, None]
    P[:, 0] = np.clip(P[:, 0], 0, w * S); P[:, 1] = np.clip(P[:, 1], 0, h * S)
    m2 = Image.new("L", (w * S, h * S), 0)
    ImageDraw.Draw(m2).polygon([tuple(p) for p in P.tolist()], fill=255)
    m2 = fx._fibre_fuzz(m2, fx.np_rng(rng), 2)
    return m2.resize((w, h), LANCZOS)


def _edge_points(sprite, ox, oy, rng, n, sides="tblr", inset=7):
    """n points ON the visible edge of an RGBA sprite placed at (ox, oy): (x, y, side). Never in empty (cut-away) corners."""
    a = np.asarray(sprite.getchannel("A")) > 128
    h, w = a.shape
    out = []
    for _ in range(n * 4):
        if len(out) >= n:
            break
        side = rng.choice(sides); f = rng.uniform(.14, .86)
        if side in "tb":
            x = int(w * f); idx = np.nonzero(a[:, x])[0]
            if idx.size == 0: continue
            y = int(idx[0] + inset) if side == "t" else int(idx[-1] - inset)
        else:
            y = int(h * f); idx = np.nonzero(a[y, :])[0]
            if idx.size == 0: continue
            x = int(idx[0] + inset) if side == "l" else int(idx[-1] - inset)
        out.append((ox + x, oy + y, side))
    return out


# =============================================================== stickers
def sticker_sprite(name, size, angle, fill=None, ring=7):
    """PNG sticker from assets/collage/stickers with a white die-cut border. None if the asset is missing."""
    sp = fx.sticker(name, int(size), fill, angle)
    if sp is None:
        return None
    pad = ring + 4
    base = Image.new("RGBA", (sp.width + 2 * pad, sp.height + 2 * pad), (0, 0, 0, 0))
    base.alpha_composite(sp, (pad, pad))
    a = base.getchannel("A")
    outline = fx.grow(a.resize((a.width * fx.SS, a.height * fx.SS), BICUBIC), ring * fx.SS).resize(a.size, LANCZOS)
    white = Image.new("RGBA", base.size, (255, 253, 247, 255)); white.putalpha(outline)
    white.alpha_composite(base)
    return white


def put_sticker(canvas, name, cx, cy, size, rng, fill=None, angle=None):
    sp = sticker_sprite(name, size, rng.uniform(-28, 28) if angle is None else angle, fill)
    if sp is not None:
        fx.place(canvas, sp, int(cx - sp.width / 2), int(cy - sp.height / 2), scale=.55)
    return sp is not None


def _random_sticker(rng, big=False):
    """(asset name, size, fill) - flowers / leaves dominate, doodles fill the rest."""
    r = rng.random()
    if r < .46:
        return rng.choice(FLOWERS), rng.randint(120, 190) if big else rng.randint(86, 140), None
    if r < .60:
        return "leaf_sprig", rng.randint(110, 170), None
    return rng.choice(DOODLES), rng.randint(70, 116), rng.choice(DOODLE_COLOURS)


# =============================================================== photos
def _clamp_xy(sprite, cx, cy, top, bottom):
    """Top-left of `sprite` centred on (cx, cy), shifted so it stays on the canvas and between y = top and bottom."""
    x = min(max(int(cx - sprite.width / 2), 6), W - 6 - sprite.width)
    y = min(max(int(cy - sprite.height / 2), top), max(top, bottom - sprite.height))
    return x, y


def _fit_inside(sprite, top, bottom):
    mw, mh = W - 12, max(120, bottom - top)
    if sprite.width > mw or sprite.height > mh:
        sprite = sprite.copy(); sprite.thumbnail((mw, mh), LANCZOS)
    return sprite


def build_photos(photos, slots, plan, rng, cs):
    """-> list of items {z, mat, sprite, x, y, cutout}. mat = (sprite, x, y) paper mount under the photo or None."""
    n = len(photos)
    want = max(1, round(n * .4))
    order = list(range(n)); rng.shuffle(order)                       # who is tried for a rembg cut-out first
    cut_ok = set()
    items = []
    top, bottom = SAFE_Y0, plan["caption_top"] + 28
    prepared = {}
    hero_area = max(s["w"] * s["h"] for s in slots)

    def grow_k(s, lo, hi):                                            # the big photo barely grows, small ones grow more and overlap it
        big = s["w"] * s["h"] >= .9 * hero_area
        return rng.uniform(1.03, 1.08) if big else rng.uniform(lo, hi)
    for i in order:                                                   # pass 1: cut-outs (stop once `want` worked)
        if len(cut_ok) >= want:
            break
        p, s = photos[i], slots[i]
        k = grow_k(s, 1.10, 1.20)
        s2 = dict(s); s2["w"], s2["h"] = round(s["w"] * k), round(s["h"] * k)
        tile, info = cs._tile(p, s2, theme="scrapbook_chaos"); s["mode"] = s2["mode"]
        prepared[i] = (tile, info)
        if info["mode"] != "crop":
            continue
        try:
            mask = cs.cut_mask(tile)
        except Exception:
            mask = None
        if mask is None:
            continue
        bb = mask.point(lambda v: 255 if v > 40 else 0).getbbox()
        if not bb or (bb[2] - bb[0]) * (bb[3] - bb[1]) < .30 * tile.width * tile.height:      # tiny subject: a cut-out would leave a hole
            continue
        res = fx.torn_die_cut(tile, mask, rng.randint(15, 20), rng)
        if res is None:
            continue
        sprite, (ox, oy) = res
        cx = s["x"] + s["w"] / 2 + (ox + sprite.width / 2 - tile.width / 2)
        cy = s["y"] + s["h"] / 2 + (oy + sprite.height / 2 - tile.height / 2)
        sprite = _fit_inside(_rot(sprite, rng.uniform(-9, 9)), top, bottom)
        x, y = _clamp_xy(sprite, cx, cy, top, bottom)
        mw, mh = int(s["w"] * rng.uniform(.9, 1.0)), int(s["h"] * rng.uniform(.86, .98))     # torn paper mount behind the cut-out
        mat = _rot(sheet((mw, mh), rng, _weighted(rng, PAPER_WEIGHTS[:3]), amp=rng.randint(8, 13)), rng.uniform(-8, 8))
        mx, my = _clamp_xy(mat, s["x"] + s["w"] / 2 + rng.randint(-26, 26), s["y"] + s["h"] / 2 + rng.randint(-26, 26), top, bottom)
        items.append(dict(z=-s["w"] * s["h"] * rng.uniform(.8, 1.2), mat=(mat, mx, my), sprite=sprite, x=x, y=y, cutout=True))
        cut_ok.add(i)
    for i in range(n):                                                # pass 2: everything else = irregular torn shape
        if i in cut_ok:
            continue
        p, s = photos[i], slots[i]
        if i in prepared:
            tile, info = prepared[i]
        else:
            k = grow_k(s, 1.08, 1.18)
            s2 = dict(s); s2["w"], s2["h"] = round(s["w"] * k), round(s["h"] * k)
            tile, info = cs._tile(p, s2, theme="scrapbook_chaos"); s["mode"] = s2["mode"]
        mask = irregular_mask(tile.size, rng, amp=rng.uniform(7, 12))
        res = fx.torn_die_cut(tile, mask, rng.randint(10, 15), rng, wobble=.5)
        if res is None:                                               # cannot happen (mask is never empty)
            continue
        sprite, (ox, oy) = res
        cx = s["x"] + s["w"] / 2 + (ox + sprite.width / 2 - tile.width / 2)
        cy = s["y"] + s["h"] / 2 + (oy + sprite.height / 2 - tile.height / 2)
        sprite = _fit_inside(_rot(sprite, rng.uniform(-8, 8)), top, bottom)
        x, y = _clamp_xy(sprite, cx, cy, top, bottom)
        mat = None
        if rng.random() < .7:                                         # paper layer under the photo (kraft / pink / cream)
            mw, mh = int(sprite.width * rng.uniform(1.0, 1.1)), int(sprite.height * rng.uniform(1.0, 1.08))
            mt = _rot(sheet((mw, mh), rng, _weighted(rng, PAPER_WEIGHTS[:3]), amp=rng.randint(8, 13)), rng.uniform(-9, 9))
            mx, my = _clamp_xy(mt, x + sprite.width / 2 + rng.randint(-22, 22), y + sprite.height / 2 + rng.randint(-22, 22), top, bottom)
            mat = (mt, mx, my)
        items.append(dict(z=-s["w"] * s["h"] * rng.uniform(.8, 1.2), mat=mat, sprite=sprite, x=x, y=y, cutout=False))
    items.sort(key=lambda it: it["z"])
    return items


# =============================================================== ransom-note title
def _letter_fonts(cs):
    """List of distinct font makers: (kind, weight). Google fonts when the asset pack has them, else Poppins / Lora."""
    pool = [(k, 700 if k == "caveat" else 800 if k.startswith("bodoni") else None)
            for k in fx.SCRAP_LETTER_FONTS + ("bodoni_i", "reenie") if fx.has_font(k)]
    pool += [("bold", None), ("serif", None), ("medium", None)]       # always distinct from the Google fonts above
    return pool


def _luma(c):
    return .299 * c[0] + .587 * c[1] + .114 * c[2]


def ransom_title(canvas, word, top, rng, cs):
    """Each letter on its own torn paper scrap: own font, own paper colour, own rotation / baseline. Returns (x0, x1)."""
    letters = [c for c in word]
    n = len(letters)
    step = min(126, (W - 2 * 44) // max(1, n))
    pool = _letter_fonts(cs); rng.shuffle(pool); queue = []; prev = None
    total = step * n; x = (W - total) // 2 + 4
    xs = []
    for ch in letters:
        if ch == " ":
            x += int(step * .7); continue
        if not queue:
            queue = pool[:]; rng.shuffle(queue)
            if queue[-1] == prev and len(queue) > 1:
                queue[0], queue[-1] = queue[-1], queue[0]
        fam = queue.pop()
        if fam == prev and queue:
            fam, queue[-1] = queue[-1], fam
        prev = fam
        tw, th = int(step * rng.uniform(.95, 1.12)), int(step * rng.uniform(1.15, 1.45))
        paper = rng.choice(TITLE_LETTER_COLOURS)
        glyph_col = (250, 244, 228) if _luma(paper) < 150 else INK
        if rng.random() < .22:                                         # coloured glyph on cream paper
            paper, glyph_col = (250, 244, 228), rng.choice([c for c in TITLE_LETTER_COLOURS[:8] if _luma(c) < 150])
        tile = tex_patch((tw, th), rng, paper, "fine").convert("RGBA")
        tile.putalpha(fx.torn_mask((tw, th), rng, amp=3, wavelength=24, band=2))
        size = int(th * .9)
        while size > 24:                                               # largest size at which the glyph fits the scrap
            f = cs.font(fam[0], size, fam[1]); bb = f.getbbox(ch)
            if bb[2] - bb[0] <= tw * .78 and bb[3] - bb[1] <= th * .74:
                break
            size -= 4
        f = cs.font(fam[0], size, fam[1]); bb = f.getbbox(ch)
        d = ImageDraw.Draw(tile)
        if rng.random() < .3:                                          # cut-out-of-a-magazine box rule
            d.rectangle((6, 6, tw - 7, th - 7), outline=glyph_col + (150,), width=2)
        d.text((tw // 2 - (bb[0] + bb[2]) // 2, th // 2 - (bb[1] + bb[3]) // 2), ch, font=f, fill=glyph_col + (255,))
        tile = _rot(tile, rng.uniform(-10, 10))
        px, py = x - (tile.width - tw) // 2, top + 4 + rng.randint(-10, 12) - (tile.height - th) // 2
        fx.place(canvas, tile, px, py, contact=(2.5, (1, 3), .55), ambient=(9, (3, 6), .30))
        xs += [px, px + tile.width]
        x += step - rng.randint(0, 6)
    return (min(xs), max(xs)) if xs else (W // 2, W // 2)


# =============================================================== handwritten note
def note_scrap(canvas, text, cx, cy, size, angle, rng, cs, kind=None, fnt_size=64, tapes=2):
    """Torn notebook / kraft / pink / cream scrap with Caveat handwriting and tape on the ends."""
    kind = kind or rng.choice(("ruled", "cream", "kraft", "pink"))
    tint, tk = PAPER[kind]
    sp = sheet(size, rng, kind, amp=5, wavelength=50)
    d = ImageDraw.Draw(sp)
    f, lines = cs._fit_font(d, text, "caveat", fnt_size, size[0] - 80, 30, 2, 700)
    lh = int(f.size * 1.05); y = (size[1] - lh * len(lines)) // 2 + lh // 2
    ink = (46, 58, 108) if kind != "kraft" else (38, 28, 22)
    for ln in lines:
        d.text((size[0] // 2, y), ln, font=f, fill=ink + (255,), anchor="mm"); y += lh
    sp = _rot(sp, angle)
    x, y0 = int(cx - sp.width / 2), int(cy - sp.height / 2)
    fx.place(canvas, sp, x, y0, **SH_SCRAP)
    for k in range(tapes):
        kind_t, tint_t = rng.choice(WASHI)
        tp = fx.tape_sprite(kind_t, rng.randint(110, 150), 36, rng.uniform(-35, 35) * (1 if k == 0 else -1), rng, tint_t)
        tx = x + (rng.randint(8, 40) if k == 0 else sp.width - rng.randint(8, 40))
        fx.place(canvas, tp, tx - tp.width // 2, y0 + 10 - tp.height // 2 + (0 if k == 0 else rng.randint(-4, 6)), **SH_TAPE)


# =============================================================== render
def render(photos, copy, rng, ctx):
    from . import collage_styles as cs                              # lazy: collage_styles imports this module lazily too
    from .captions import pick_caption, FALLBACKS

    canvas = fx.paper_bg((246, 238, 220), "crumple", .85)           # cream base; every part of it is covered by paper layers
    background(canvas, rng)

    photos, slots, plan = cs._plan(ctx, photos, caption=CAPTION, gap=0, margin_x=MARGIN_X - 24)
    plan["min_gap"] = 0                                             # photos overlap on purpose (validator reads this)
    items = build_photos(photos, slots, plan, rng, cs)

    # --- sticker anchors: points on the photos' own edges (so they overlap them), plus the paper-only top / bottom bands
    edge = []
    for it in items:
        edge += _edge_points(it["sprite"], it["x"], it["y"], rng, 3)
    bands = [(rng.randint(60, W - 60), rng.randint(40, 215)) for _ in range(4)] + \
            [(rng.randint(60, W - 60), rng.randint(1700, 1880)) for _ in range(4)] + \
            [(rng.choice((22, W - 22)), rng.randint(300, 1300)) for _ in range(2)]
    rng.shuffle(edge)
    behind = edge[:3]                                               # these stickers sit UNDER the photos, peeking out
    front = edge[3:] + [(x, y, None) for x, y in bands]

    for x, y, _s in behind:
        name, size, fill = _random_sticker(rng, big=True)
        if not put_sticker(canvas, name, x, y, size, rng, fill):
            cs.doodle(canvas, "flower", x, y, size, (247, 226, 140), rng)

    # --- photos (mount first, then the photo; small first, so big ones end up on top)
    for it in items:                                                  # all paper mounts first: a mount never buries another photo
        if it["mat"]:
            mt, mx, my = it["mat"]
            fx.place(canvas, mt, mx, my, **SH_SCRAP)
    for it in items:
        fx.place(canvas, it["sprite"], it["x"], it["y"], **SH_PHOTO)

    # --- tape on the photos: on the real edge of each shape, at corners
    for it in items:
        sides = "tt" + ("b" if it["cutout"] else "l")
        for (x, y, side), sgn in zip(_edge_points(it["sprite"], it["x"], it["y"], rng, 2, sides=sides, inset=10), (-1, 1)):
            kind, tint = rng.choice(WASHI)
            tp = fx.tape_sprite(kind, rng.randint(150, 200), rng.randint(38, 46), sgn * rng.uniform(14, 38), rng, tint)
            fx.place(canvas, tp, x - tp.width // 2, y - tp.height // 2, **SH_TAPE)

    for x, y, _s in front:
        name, size, fill = _random_sticker(rng)
        if not put_sticker(canvas, name, x, y, size, rng, fill):
            cs.doodle(canvas, "sparkle", x, y, size, (255, 214, 80), rng)

    # --- two small handwritten tags on photo edges (the vision caption of that photo, or a neutral fallback)
    used = set(); start = rng.randrange(len(FALLBACKS))
    for k, it in enumerate(items[-2:]):
        pts = _edge_points(it["sprite"], it["x"], it["y"], rng, 1, sides="b", inset=-4)
        if not pts:
            continue
        x, y, _s = pts[0]
        tag = pick_caption(None, used, start + k)
        f_ = cs.font("caveat", 46, 700)
        w_, h_ = int(ImageDraw.Draw(Image.new("L", (4, 4))).textlength(tag, font=f_)) + 120, 76     # one line, always readable
        cy = min(y + 4, plan["caption_top"] - 30)
        note_scrap(canvas, tag, min(max(x, w_ // 2 + 12), W - w_ // 2 - 12), cy, (w_, h_), rng.uniform(-9, 9), rng, cs,
                   kind=rng.choice(("kraft", "pink", "cream")), fnt_size=46, tapes=1)

    # --- ransom-note title + handwritten note, on paper only (below the photos, inside y .. 1670)
    top = plan["caption_top"]
    word = cs._dt(photos[0]).strftime("%b").lower() + " dump"
    x0, x1 = ransom_title(canvas, word, top + 6, rng, cs)
    text = str(copy.get("text", "")).lower() or "little moments, loud memories"
    note_scrap(canvas, text, W // 2 + rng.randint(-10, 10), top + 172 + 74, (W - 2 * MARGIN_X, 128), rng.uniform(-1.8, 1.8), rng, cs,
               kind="ruled", fnt_size=64, tapes=2)
    for x, y in ((x0 + 30, top - 6), (x1 - 40, top + 158)):          # a flower on a corner of the title (touches, does not hide a letter)
        put_sticker(canvas, rng.choice(FLOWERS), min(max(x, 56), W - 56), y, rng.randint(84, 108), rng)
    return canvas
