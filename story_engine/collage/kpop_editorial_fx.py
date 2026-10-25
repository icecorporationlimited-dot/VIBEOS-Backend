"""K-Pop editorial = Vogue-cover collage look. Used ONLY by collage_styles.kpop_editorial (no other style touches this).

Per tile (reference: B&W photos, big lavender-purple gradient serif masthead, small serif names list, subject in front)
  1. photo -> B&W + high contrast (own grade, strong S-curve; rembg still sees the colour photo)
  2. big gradient serif masthead ("VIBEOS" / "VIBE" on narrow tiles) is drawn BEHIND the subject
  3. rembg mask pastes the subject back over the masthead (masthead is placed so the head overlaps its lower part)
  4. small serif names list goes on the left / right sides under the masthead

The cut-out is REQUIRED for step 2-3. If rembg is missing / fails / is skipped (budget, COLLAGE_CUTOUTS=0, contain mode)
or the mask is unusable (subject hides most of the masthead, or mask covers the whole frame), the tile uses a SEPARATE
fallback: the masthead is NOT drawn behind the subject; it runs vertically along the calmer side edge and the names
list sits on the opposite side. Nothing is ever drawn behind a subject without a real mask.
"""
import logging

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from . import collage_fx as fx

log = logging.getLogger(__name__)

# (top, bottom) of the masthead gradient: lavender -> purple; odd tiles lean orchid
PALETTES = (((214, 190, 255), (104, 64, 226)),
            ((240, 196, 255), (150, 84, 232)))
BORDER = ((123, 108, 255, 235), (205, 110, 255, 235))      # same tile frame colours as before
MASTHEAD_ALPHA = 244
DEFAULT_NAMES = ("VIBEOS", "EDITORIAL", "ISSUE 01", "SPOTLIGHT", "ON REPEAT", "MOOD")
MAX_HIDDEN = 0.60               # more than 60 % of the masthead under the subject = unreadable -> side fallback
MAX_SUBJECT_AREA = 0.85         # mask covering (almost) the whole tile = failed cut-out


# ---------------------------------------------------------------- B&W high contrast
def bw(im):
    """Greyscale, auto-levelled, strong S-curve + a little extra contrast. RGB in -> RGB (neutral grey) out."""
    g = ImageOps.autocontrast(ImageOps.grayscale(im.convert("RGB")), cutoff=2)
    lut = []
    for v in range(256):
        s = v / 255.0
        s = s * 0.45 + (s * s * (3 - 2 * s)) * 0.55          # S-curve: deep blacks, clean highlights
        lut.append(max(0, min(255, int(round((0.02 + s * 0.98) * 255)))))
    return ImageEnhance.Contrast(g.point(lut)).enhance(1.18).convert("RGB")


# ---------------------------------------------------------------- small helpers
def names_list(copy):
    """Up to 6 short UPPERCASE entries for the small serif list: hashtags + longer words of the line + defaults."""
    out, seen = [], set()

    def add(x, min_len):
        x = "".join(ch for ch in str(x).upper() if ch.isalnum() or ch in " &'-").strip()
        if len(x) >= min_len and x not in seen:
            seen.add(x); out.append(x)
    for h in (copy.get("hashtags") or []):
        add(str(h).lstrip("#"), 3)
    for w in str(copy.get("text", "")).split():
        add(w, 4)
    for d in DEFAULT_NAMES:
        add(d, 3)
    return out[:6]


def _gradient(size, top, bot):
    """RGB image, mostly vertical lavender->purple blend with a little diagonal drift."""
    w, h = max(1, size[0]), max(1, size[1])
    ty = np.linspace(0, 1, h, dtype=np.float32)[:, None]
    tx = np.linspace(0, 1, w, dtype=np.float32)[None, :]
    t = (0.78 * ty + 0.22 * tx)[:, :, None]
    arr = np.asarray(top, np.float32)[None, None, :] * (1 - t) + np.asarray(bot, np.float32)[None, None, :] * t
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")


def _gradient_layer(size, mask, top, bot, alpha=MASTHEAD_ALPHA):
    """RGBA layer (`size`) that shows the gradient only where `mask` (L, same size) is white. Returns (layer, bbox)."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    bb = mask.getbbox()
    if not bb:
        return layer, None
    grad = _gradient((bb[2] - bb[0], bb[3] - bb[1]), top, bot).convert("RGBA")
    grad.putalpha(mask.crop(bb).point(lambda v: v * alpha // 255))
    layer.paste(grad, bb[:2])                      # replace (not blend) so edge colours are not darkened
    return layer, bb


def _tracked_width(d, text, f, track):
    return sum(d.textlength(c, font=f) for c in text) + track * (len(text) - 1)


def _names_block(items, f, track, line_h, align):
    """Small serif list with a soft shadow baked in -> RGBA image. align: 'l' or 'r'."""
    probe = ImageDraw.Draw(Image.new("L", (8, 8)))
    pad = 8
    bw_ = int(max(_tracked_width(probe, s, f, track) for s in items)) + 2 * pad
    bh_ = line_h * len(items) + 2 * pad
    sh = Image.new("RGBA", (bw_, bh_), (0, 0, 0, 0)); tx = Image.new("RGBA", (bw_, bh_), (0, 0, 0, 0))
    ds, dt = ImageDraw.Draw(sh), ImageDraw.Draw(tx)
    for i, s in enumerate(items):
        wid = _tracked_width(probe, s, f, track)
        x = pad if align == "l" else bw_ - pad - wid
        y = pad + int(line_h * (i + 0.8))
        for c in s:
            ds.text((x + 1, y + 1), c, font=f, fill=(0, 0, 0, 175), anchor="ls")
            dt.text((x, y), c, font=f, fill=(255, 255, 255, 238), anchor="ls")
            x += probe.textlength(c, font=f) + track
    sh = sh.filter(ImageFilter.GaussianBlur(1.6))
    sh.alpha_composite(tx)
    return sh


def _put(t, block, x, y):
    """alpha_composite `block` at (x, y), clamped inside tile `t`."""
    x = int(max(0, min(t.width - block.width, x))); y = int(max(0, min(t.height - block.height, y)))
    t.alpha_composite(block, dest=(x, y))


def _list_font(cs, tw):
    fs = max(15, int(tw * 0.030))
    return cs.font("playfair", fs, 500), fs


def _masthead_font(cs, tw, th):
    """(word, font): VIBEOS (VIBE on narrow tiles), largest bold Didone that fits ~90 % of the width / 30 % of the height."""
    narrow = tw < 600
    word = "VIBE" if narrow else "VIBEOS"
    fs = int(tw * (.62 if narrow else .30))
    f = cs.font("bodoni", fs, 700)
    while fs > 60:
        bb = f.getbbox(word, anchor="ls")
        if bb[2] - bb[0] <= tw * .90 and bb[3] - bb[1] <= th * .30:
            break
        fs -= 6; f = cs.font("bodoni", fs, 700)
    return word, f


def _soften(mask):
    """Tighten rembg's soft edge (levels), pull in 1 px against halos, then a light blur."""
    m = mask.point(lambda v: 0 if v < 35 else 255 if v > 220 else int((v - 35) * 255 / 185))
    return m.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(1.1))


def _usable_mask(mask):
    """True when the cut-out looks like a real subject (not empty, not the whole frame)."""
    a = np.asarray(mask.resize((64, 64)), np.float32) / 255.0
    return 0.06 <= float(a.mean()) <= MAX_SUBJECT_AREA


def _hidden_fraction(text_mask, subj_mask):
    t = np.asarray(text_mask, np.float32) / 255.0
    s = np.asarray(subj_mask, np.float32) / 255.0
    tot = float(t.sum())
    return float((t * s).sum() / tot) if tot else 1.0


# ---------------------------------------------------------------- main look: masthead BEHIND the subject
def _behind(cs, base, subj, k, copy_names, palette):
    """Masthead behind the cut-out subject + names list on both sides. Returns (tile, True) or (None, False) if the
    mask makes the masthead unreadable (caller then uses the side fallback)."""
    tw, th = base.size
    top, bot = palette
    word, f = _masthead_font(cs, tw, th)
    bb = f.getbbox(word, anchor="ma")
    t_off, mh = bb[1], bb[3] - bb[1]
    sbb = subj.point(lambda v: 255 if v > 128 else 0).getbbox()
    head_top = sbb[1] if sbb else None
    lo, hi = 0.02 * th, 0.14 * th
    cands = []
    if head_top is not None:                               # head overlaps the lower ~40 % of the masthead
        cands.append(min(hi, max(lo, head_top - 0.6 * mh)))
    cands.append(lo)
    for y_top in cands:
        mask = Image.new("L", (tw, th), 0)
        ImageDraw.Draw(mask).text((tw // 2, int(y_top - t_off)), word, font=f, fill=255, anchor="ma")
        hid = _hidden_fraction(mask, subj)
        if hid <= MAX_HIDDEN:
            break
    else:
        log.info("kpop: subject hides %.0f%% of the masthead -> side fallback", hid * 100)
        return None, False
    t = base.copy()
    layer, mbb = _gradient_layer((tw, th), mask, top, bot)
    t.alpha_composite(layer)
    t.paste(base, (0, 0), subj)                            # subject back over the masthead
    # small serif list on the sides, under the masthead (drawn after the subject so it stays readable)
    f2, fs = _list_font(cs, tw)
    line_h = int(fs * 1.9); per_side = 2 if tw < 600 else 3
    y0 = (mbb[3] if mbb else int(y_top + mh)) + int(0.022 * th)
    margin = int(tw * 0.05)
    left = copy_names[:per_side]; right = copy_names[per_side:2 * per_side] or copy_names[:per_side]
    if tw >= 340 and y0 + line_h * per_side < th * 0.6:
        _put(t, _names_block(left, f2, fs * 0.16, line_h, "l"), margin - 8, y0)
        blk = _names_block(right, f2, fs * 0.16, line_h, "r")
        _put(t, blk, tw - margin - blk.width + 8, y0)
    return t, True


# ---------------------------------------------------------------- fallback: NO cut-out -> text beside the subject
def _calmer_side(tile):
    """'l' or 'r': the edge strip with less detail (less likely to hold the subject)."""
    g = np.asarray(ImageOps.grayscale(tile).resize((160, max(8, int(160 * tile.height / tile.width)))), np.float32)
    strip = max(4, int(g.shape[1] * 0.20))
    def detail(a):
        return float(np.abs(np.diff(a, axis=0)).mean() + np.abs(np.diff(a, axis=1)).mean())
    return "l" if detail(g[:, :strip]) <= detail(g[:, -strip:]) else "r"


def _side(cs, base, k, copy_names, palette):
    """Vertical masthead along the calmer edge, names list on the opposite edge. Returns (tile, side, strip_w)."""
    tw, th = base.size
    top, bot = palette
    side = _calmer_side(base.convert("RGB"))
    word = "VIBE" if tw < 600 else "VIBEOS"
    fs = int(tw * 0.22)
    f = cs.font("bodoni", fs, 700)
    while fs > 40:
        bb = f.getbbox(word, anchor="ls")
        if bb[2] - bb[0] <= th * 0.86 and bb[3] - bb[1] <= tw * 0.17:
            break
        fs -= 6; f = cs.font("bodoni", fs, 700)
    bb = f.getbbox(word, anchor="ls")
    lw, lh = bb[2] - bb[0], bb[3] - bb[1]
    horiz = Image.new("L", (lw + 8, lh + 8), 0)
    ImageDraw.Draw(horiz).text((4 - bb[0], 4 - bb[1]), word, font=f, fill=255, anchor="ls")
    horiz = horiz.crop(horiz.getbbox() or (0, 0, 1, 1))
    vert = horiz.rotate(90 if side == "l" else -90, expand=True, resample=Image.Resampling.BICUBIC)
    layer, _ = _gradient_layer(vert.size, vert, top, bot)
    t = base.copy()
    m = int(tw * 0.035)
    x = m if side == "l" else tw - m - vert.width
    y = int(th * 0.5 - vert.height / 2)
    t.alpha_composite(layer, dest=(max(0, x), max(0, min(th - vert.height, y))))
    f2, fs2 = _list_font(cs, tw)
    n = 5 if th >= 700 else 3
    blk = _names_block(copy_names[:n], f2, fs2 * 0.16, int(fs2 * 1.9), "r" if side == "l" else "l")
    other_x = tw - m - blk.width + 8 if side == "l" else m - 8
    if tw >= 340:
        _put(t, blk, other_x, int(th * 0.05))
    return t, side, vert.width + m


# ---------------------------------------------------------------- style entry point
def render(photos, copy, rng, ctx):
    """Story canvas (RGBA) for the kpop_editorial theme. Same slot planner / tile placement as every other style."""
    from . import collage_styles as cs                      # lazy: collage_styles imports this module's caller
    canvas = fx.paper_bg((242, 242, 246), "fine", .6)
    photos, slots, plan = cs._plan(ctx, photos)
    names = names_list(copy)
    for k, (p, s) in enumerate(zip(photos, slots)):
        tw, th = s["w"], s["h"]
        raw, info = cs._tile(p, s)                          # ungraded: rembg gets the colour photo
        palette = PALETTES[k % 2]
        col = BORDER[k % 2]
        subj = None
        if info["mode"] == "crop":
            m = cs.cut_mask(raw)                            # None when rembg is missing / fails / budget is short
            if m is not None and _usable_mask(m):
                subj = _soften(m)
        base = bw(raw).convert("RGBA")
        rot = names[k % len(names):] + names[:k % len(names)]
        t, ok = (None, False)
        if subj is not None:
            t, ok = _behind(cs, base, subj, k, rot, palette)
        side, strip = None, 0
        if not ok:                                          # separate fallback: text beside the subject, never behind
            t, side, strip = _side(cs, base, k, rot, palette)
        s["kpop_masthead"] = "behind" if ok else "side"
        if k == 0:                                          # the headline lives on the hero tile only
            d = ImageDraw.Draw(t)
            x0 = 30 + (strip + 6 if side == "l" else 0)
            max_w = tw - 60 - (strip + 6 if side else 0)
            tf, lines = cs._fit_font(d, str(copy.get("text", "")).lower(), "playfair_i", min(64, int(th * .11)), max_w, 28, 2)
            d.text((x0, th - 30 - len(lines) * int(tf.size * 1.15)), "\n".join(lines), font=tf, fill=col)
        ImageDraw.Draw(t).rectangle((0, 0, tw - 1, th - 1), outline=col, width=5)
        fx.place(canvas, t, s["x"], s["y"], contact=(2.5, (1, 3), .32), ambient=(16, (0, 9), .2))
    return canvas
