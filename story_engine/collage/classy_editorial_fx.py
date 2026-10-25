"""Classy Editorial = quiet gallery-page look. Used ONLY by collage_styles.classy_editorial (no other style touches it).

Look
  * flat bone / off-white background #F2F1ED, lots of empty space (tiles cover only a small part of the canvas)
  * tiny tiles, each with a 1 px hairline border and an 8 px paper mat; uneven "editorial mosaic" (hand-set boxes,
    staggered sizes - not a regular grid)
  * small serif captions (Bodoni Moda caps for numerals / masthead, Lora italic for words), ONE vertical line of text
  * muted grade: photos slightly desaturated, warm highlights, faint black lift
  * no doodles, no tape, no torn edges; the only shadow is blur 20 / opacity 10 %

Grid: every position and size is a multiple of U = 8 px (tile x / y / w / h, margins, gaps, text baselines, rules).
Tile sizes are k * (rw, rh) * 8 px, so the planner's fixed ratios (3:4, 4:5, 1:1, 5:4, 4:3) stay exact.

Contract with the rest of the collage pipeline: same plan dict as collage_layout_planner.plan_slots() (slots carry
x / y / w / h / ratio_name / ratio / orientation / photo_aspect / photo_index / asset_id), so collage_validator
.check_plan() applies unchanged: slots stay inside the story safe zone (y 250 .. 1670) and >= 24 px apart (here >= 32).
"""
import itertools
import math

from PIL import Image, ImageDraw, ImageEnhance

from . import collage_fx as fx
from .collage_layout_planner import (RATIOS, SAFE_Y0, SAFE_Y1, _ALLOWED, _crop_ok, orientation, photo_size)

W, H = 1080, 1920
U = 8                                       # the grid unit (px)
BG = (242, 241, 237)                        # #F2F1ED
CARD = (249, 248, 245)                      # tile mat: a hair lighter than the page
INK = (44, 41, 37)
INK2 = (112, 106, 98)                       # secondary text
HAIR = (120, 114, 104)                      # 1 px hairline
MAT = 1 * U                                 # paper mat between hairline and photo
LABEL = 6 * U                               # strip under every tile for its numeral / caption (48 px)
MIN_GAP = 4 * U                             # box separation (32 px >= validator's 24)
MARGIN = 12 * U                             # 96 px page margin, left and right
RULE_TOP = 38 * U                           # 304: hairline under the masthead
CAPTION_Y = 184 * U                         # 1472: first line of the caption block
RULE_BOTTOM = 200 * U                       # 1600
_UNITS = {"3:4": (3, 4), "4:5": (4, 5), "1:1": (1, 1), "5:4": (5, 4), "4:3": (4, 3)}

# Boxes (x, y, w, h, anchor-x, anchor-y) in px, all multiples of 8. h includes the LABEL strip. First box = hero.
# Content lives in x 96..984, y 352..1440; the tile is fitted (largest exact-ratio tile) inside its box.
TEMPLATES = {
    5: [
        [(96, 352, 352, 496, "l", "t"), (560, 352, 224, 328, "l", "t"), (808, 536, 176, 264, "r", "t"),
         (240, 960, 264, 376, "l", "t"), (632, 1000, 200, 288, "l", "b")],
        [(344, 352, 352, 496, "l", "t"), (96, 464, 184, 288, "l", "t"), (736, 640, 248, 360, "r", "t"),
         (96, 1008, 296, 408, "l", "t"), (488, 1080, 200, 288, "l", "t")],
    ],
    4: [
        [(96, 352, 368, 520, "l", "t"), (592, 480, 232, 344, "l", "t"), (216, 984, 256, 376, "l", "t"),
         (656, 1040, 200, 280, "l", "t")],
        [(472, 352, 360, 520, "l", "t"), (96, 520, 248, 336, "l", "t"), (592, 984, 240, 360, "r", "t"),
         (128, 1008, 232, 312, "l", "t")],
    ],
    3: [
        [(96, 384, 400, 560, "l", "t"), (624, 560, 248, 344, "l", "t"), (264, 1056, 264, 392, "l", "t")],
        [(584, 352, 400, 560, "r", "t"), (96, 592, 240, 344, "l", "t"), (368, 1032, 264, 392, "l", "t")],
    ],
}


# ------------------------------------------------------------------ colour
def _smooth(a, b, v):
    t = min(1.0, max(0.0, (v - a) / (b - a)))
    return t * t * (3 - 2 * t)


# per-channel (R, G, B) offsets: warm highlights, barely-cool shadows, small matte lift
_HI = (7, 3, -8)
_LO = (-1, 0, 2)
_LIFT = 8


def grade(im):
    """Muted grade: ~20 % less saturation, flatter contrast, matte blacks, warm highlights."""
    im = ImageEnhance.Color(im.convert("RGB")).enhance(0.80)
    im = ImageEnhance.Contrast(im).enhance(0.97)
    lut = []
    for c in range(3):
        for v in range(256):
            s = v / 255.0
            out = _LIFT + v * (1 - _LIFT / 255.0)
            out += _smooth(0.45, 1.0, s) * _HI[c] + (1 - _smooth(0.0, 0.5, s)) * _LO[c]
            lut.append(max(0, min(255, int(round(out)))))
    return im.point(lut)


# ------------------------------------------------------------------ layout
def _snap(v):
    return int(v) // U * U


def _fit(box, ratio_name):
    """Largest exact-ratio tile (w, h) inside the box minus its label strip, or None."""
    rw, rh = _UNITS[ratio_name]
    k = min(box[2] // (rw * U), (box[3] - LABEL) // (rh * U))
    return None if k < 1 else (rw * U * k, rh * U * k)


def _best(i, box, aspect, orient, photo, size, cache):
    """(cost, ratio_name, (w, h)) for photo i in this box: closest fixed ratio that fills the box, contain penalised."""
    best = None
    for name in _ALLOWED[orient]:
        wh = _fit(box, name)
        if wh is None:
            continue
        c = abs(math.log(aspect / RATIOS[name])) + (0.0 if _crop_ok(cache, i, name, photo, size) else 0.8)
        c += 0.6 * (1 - wh[0] * wh[1] / (box[2] * (box[3] - LABEL)))
        if best is None or c < best[0]:
            best = (c, name, wh)
    return best


def _mirror(tpl):
    return [(W - x - w, y, w, h, {"l": "r", "r": "l", "c": "c"}[ax], ay) for x, y, w, h, ax, ay in tpl]


def plan(photos, rng):
    """Assign photos to the mosaic boxes -> the usual plan dict (see module doc)."""
    n = len(photos)
    if n not in TEMPLATES:
        raise ValueError("classy_editorial needs 3-5 photos")
    sizes = [photo_size(p) for p in photos]
    aspects = [w / h for w, h in sizes]
    orients = [orientation(a) for a in aspects]
    cache = {}
    order = list(range(len(TEMPLATES[n])))
    rng.shuffle(order)
    mirrored = rng.random() < 0.5
    win = None
    for ti in order:                                                  # first template that can hold all photos
        tpl = _mirror(TEMPLATES[n][ti]) if mirrored else TEMPLATES[n][ti]
        for perm in itertools.permutations(range(n)):                 # perm[box] = photo index
            fits = [_best(i, tpl[b], aspects[i], orients[i], photos[i], sizes[i], cache) for b, i in enumerate(perm)]
            if any(f is None for f in fits):
                continue
            cost = sum(f[0] for f in fits) + 0.03 * sum(abs(b - i) for b, i in enumerate(perm))
            if win is None or cost < win[0]:
                win = (cost, ti, tpl, perm, fits)
        if win:
            break
    if win is None:
        raise ValueError("no classy_editorial layout fits these photos")
    _c, ti, tpl, perm, fits = win
    slots = []
    for b, (i, (_cost, name, (tw, th))) in enumerate(zip(perm, fits)):
        x, y, bw, bh, ax, ay = tpl[b]
        ah = bh - LABEL
        tx = x if ax == "l" else x + bw - tw if ax == "r" else x + _snap((bw - tw) / 2)
        ty = y if ay == "t" else y + ah - th if ay == "b" else y + _snap((ah - th) / 2)
        slots.append({"x": tx, "y": ty, "w": tw, "h": th, "ratio_name": name, "ratio": RATIOS[name],
                      "photo_index": b, "orientation": orients[i], "photo_aspect": round(aspects[i], 3),
                      "asset_id": photos[i].get("asset_id")})
    return {"photos": [photos[i] for i in perm], "slots": slots, "layout": "mosaic-%d%s" % (ti, "m" if mirrored else ""),
            "safe": (0, SAFE_Y0, W, SAFE_Y1), "area": (MARGIN, 44 * U, W - MARGIN, 180 * U),
            "header_top": SAFE_Y0, "caption_top": CAPTION_Y, "gap": MIN_GAP, "scale": 1.0, "contain_slots": 0}


# ------------------------------------------------------------------ type
def _tracked(d, xy, text, f, fill, track=0, anchor="l"):
    """Letter-spaced single line (PIL has no tracking). anchor: l / r. Returns the line width."""
    ws = [d.textlength(ch, font=f) for ch in text]
    total = sum(ws) + track * max(0, len(text) - 1)
    x, y = xy
    if anchor == "r":
        x -= total
    for ch, w_ in zip(text, ws):
        d.text((x, y), ch, font=f, fill=fill)
        x += w_ + track
    return total


def _vertical(canvas, text, f, fill, x, y, track=0):
    """One line of text rotated 90 deg clockwise (reads top to bottom); its top end sits at y, its left edge at x."""
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    tw = int(sum(probe.textlength(ch, font=f) for ch in text) + track * (len(text) - 1)) + U
    th = int(f.size * 1.5)
    layer = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    _tracked(ImageDraw.Draw(layer), (0, 0), text, f, fill + (255,), track)
    layer = layer.rotate(-90, expand=True)
    fx.blit(canvas, layer, x, y)


def render(photos, copy, rng, ctx):
    """Story canvas (RGBA) for the classy_editorial theme."""
    from . import collage_styles as cs                       # lazy: collage_styles imports this module's caller
    from .captions import pick_caption
    canvas = Image.new("RGBA", (W, H), BG + (255,))
    pl = plan(photos, rng)
    ctx["plan"] = pl
    photos, slots = pl["photos"], pl["slots"]
    d = ImageDraw.Draw(canvas)
    caps = cs.font("bodoni", 19)                              # small caps: numerals, masthead
    ital = cs.font("serif", 20)                               # small italic words (Lora)
    when = cs._dt(photos[0])

    # masthead: one hairline, issue number left, wordmark right
    _tracked(d, (MARGIN, 33 * U), "N° " + when.strftime("%d"), caps, INK, 3)
    _tracked(d, (W - MARGIN, 33 * U), "FOLK.", caps, INK, 3, "r")
    d.line((MARGIN, RULE_TOP, W - MARGIN, RULE_TOP), fill=HAIR, width=1)

    # the single vertical line of text, in the right margin, top aligned to the first tile row
    _vertical(canvas, when.strftime("%B %Y").upper(), caps, INK2, 128 * U, 44 * U, 5)

    used, n = set(), len(photos)
    for k, (p, s) in enumerate(zip(photos, slots)):
        inner, info = cs._tile(p, s, inset=MAT)               # photo cropped to the tile minus the mat
        s["mode"] = info["mode"]
        card = Image.new("RGBA", (s["w"], s["h"]), CARD + (255,))
        card.paste(grade(inner), (MAT, MAT))
        ImageDraw.Draw(card).rectangle((0, 0, s["w"] - 1, s["h"] - 1), outline=HAIR + (255,), width=1)
        # the only shadow: blur 20, opacity 10 %, dropped one grid unit
        fx.place(canvas, card, s["x"], s["y"], contact=(1, (0, 0), 0), ambient=(20, (0, U), 0.10))
    d = ImageDraw.Draw(canvas)
    for k, (p, s) in enumerate(zip(photos, slots)):
        ly = s["y"] + s["h"] + 2 * U
        num = "%02d" % (k + 1)
        nw = _tracked(d, (s["x"], ly), num, caps, INK, 2)
        word = pick_caption(p.get("caption"), used, k)
        if d.textlength(word, font=ital) + nw + 2 * U <= s["w"]:
            d.text((s["x"] + s["w"], ly - 1), word, font=ital, fill=INK2, anchor="ra")

    # headline: small italic serif on the 40 px baseline grid, bottom-left, in the open space
    t = str(copy.get("text", "")).strip()
    t = t[:1].upper() + t[1:]
    f, lines = cs._fit_font(d, t, "bodoni_i", 32, 62 * U, 24, 2)
    for j, ln in enumerate(lines):
        d.text((MARGIN, CAPTION_Y + j * 5 * U), ln, font=f, fill=INK)
    d.line((MARGIN, RULE_BOTTOM, W - MARGIN, RULE_BOTTOM), fill=HAIR, width=1)

    pl["contain_slots"] = sum(1 for s in slots if s.get("mode") == "contain")
    return canvas
