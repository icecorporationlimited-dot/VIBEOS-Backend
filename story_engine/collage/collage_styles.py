"""Six story collage styles (1080x1920), modelled on the Aesthetic Styles Guide:
playful_hype, kpop_editorial, photo_dump, streetwear_baddie, classy_editorial, scrapbook_chaos.

Rendering quality (see collage_fx.py for the primitives)
  * masks / edges are built at 2x and downsampled (anti-aliased); only cards / masks, never the whole canvas
  * torn edge = fractal-noise contour; two layers: fibre-fuzz white paper rim outside, the photo's own ragged edge inside
  * shadow = tight contact (blur ~3.5) + soft ambient (blur ~22), warm brown, multiply
  * grain = gaussian noise over the canvas (4-8 %), no sparse dots
  * tape, doodles, paper, torn scraps are LOADED from assets/collage/ (PNG / JPG); the old drawn versions remain only
    as a fallback when the asset pack is missing
  * every style has its own colour grade for its photos (collage_fx.GRADES)
  * fonts (Google Fonts, assets/collage/fonts/, fetched by tools/fetch_collage_fonts.py): handwriting Caveat / Reenie
    Beanie / Gaegu, typewriter Special Elite, magazine serif Playfair Display / Bodoni Moda, street Anton / Bebas Neue /
    Permanent Marker, scrapbook letters = a different font on every letter. Missing font -> Poppins / Lora.

Layout: no style has its own slot coordinates. Every style asks collage_layout_planner.plan_slots(), which
(1) matches photo orientation to fixed-ratio slots, (2) keeps >= 24 px between slots, (3) keeps photos AND text out of
the top / bottom 250 px (Instagram story UI). Styles only decorate the slots; decoration never grows beyond its slot.
"""
from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageFilter, ImageEnhance
from pathlib import Path
from datetime import datetime, timezone
import io, math, os, random, logging

from . import collage_fx as fx

log = logging.getLogger(__name__)
W, H = 1080, 1920
_FONTS = Path(__file__).resolve().parents[2] / "fonts"
_FILES = {"bold": "Poppins-Bold.ttf", "medium": "Poppins-Medium.ttf", "light": "Poppins-Light.ttf",
          "serif": "Lora-Italic-Variable.ttf"}
THEMES = ("playful_hype", "kpop_editorial", "photo_dump", "streetwear_baddie", "classy_editorial", "scrapbook_chaos")
GRAIN = {"playful_hype": 0.04, "kpop_editorial": 0.045, "photo_dump": 0.08, "streetwear_baddie": 0.08,
         "classy_editorial": 0.02, "scrapbook_chaos": 0.06}                      # gaussian grain opacity per style


# ------------------------------------------------------------------ helpers
def font(kind, size, weight=None):
    """kind: bold / medium / light / serif (Poppins, Lora) or a collage_fx.FAMILIES key (caveat, reenie, gaegu, elite,
    playfair, playfair_i, bodoni, bodoni_i, anton, bebas, marker)."""
    if kind in fx.FAMILIES:
        return fx.family_font(kind, size, weight)
    for p in (_FONTS / _FILES[kind], Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")):
        try:
            return ImageFont.truetype(str(p), size)
        except Exception:
            continue
    return ImageFont.load_default()


def _load(p):
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(p["bytes"]))).convert("RGB"); im.thumbnail((1600, 1600)); return im


def fit(p, size, report=False):
    """Slot-sized RGB tile. Subject (face / whole subject) stays fully inside; otherwise the photo is shown whole
    (contain) on a blurred copy. report=True -> (tile, info) with info["mode"] in ("crop", "contain")."""
    from .subject_crop import render_slot
    img, info = render_slot(_load(p), size, p.get("subject_region"))
    return (img, info) if report else img


def arranged(photos, sizes):
    """Reorder photos so every subject fits its slot's ratio where possible (else that slot uses contain mode)."""
    from .subject_crop import arrange
    return arrange(photos, sizes)


blit = fx.blit


def cut_mask(tile):
    """Subject mask (L, same size as tile) via rembg, or None. Never raises."""
    if os.getenv("COLLAGE_CUTOUTS", "1").lower() in ("0", "false", "off", "no"):
        return None
    from . import budget
    if not budget.have(35):                                        # cutouts are decoration: skip when late
        log.info("cutout skipped: %.0fs budget left", budget.remaining())
        return None
    try:
        from . import collage_renderer as cr
        from rembg import remove, new_session
        if cr._SESSION is None:
            cr._SESSION = new_session(os.getenv("COLLAGE_REMBG_MODEL", "u2netp"))
        small = tile.copy(); small.thumbnail((384, 384)); raw = io.BytesIO(); small.save(raw, "PNG")
        a = Image.open(io.BytesIO(remove(raw.getvalue(), session=cr._SESSION))).convert("RGBA").getchannel("A")
        if sum(a.resize((16, 16)).tobytes()) < 0.06 * 255 * 256:     # < 6% subject => failed cut
            return None
        return a.resize(tile.size, Image.Resampling.LANCZOS)
    except Exception as e:
        log.info("cutout skipped: %s", e); return None


die_cut = fx.die_cut


def wrap(draw, text, fnt, max_w, max_lines=3):
    lines, cur = [], ""
    for w_ in str(text).split():
        t = (cur + " " + w_).strip()
        if draw.textlength(t, font=fnt) <= max_w or not cur: cur = t
        else: lines.append(cur); cur = w_
    if cur: lines.append(cur)
    return lines[:max_lines]


# --- drawn doodles: ONLY the fallback when assets/collage/stickers/ is missing
def sparkle(d, cx, cy, r, fill, outline=None):
    pts = []
    for k in range(8):
        a = -math.pi / 2 + k * math.pi / 4; rr = r if k % 2 == 0 else r * .26
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    d.polygon(pts, fill=fill, outline=outline)


def heart(d, cx, cy, r, fill):
    d.ellipse((cx - r, cy - r * .9, cx, cy + r * .1), fill=fill); d.ellipse((cx, cy - r * .9, cx + r, cy + r * .1), fill=fill)
    d.polygon([(cx - r, cy - r * .3), (cx + r, cy - r * .3), (cx, cy + r * 1.05)], fill=fill)


def sun(d, cx, cy, r, fill):
    pts = [(cx + (r * .12 + r * .5 * t / 40) * math.cos(t / 40 * 3.2 * math.pi * 2 / 2), cy + (r * .12 + r * .5 * t / 40) * math.sin(t / 40 * 3.2 * math.pi * 2 / 2)) for t in range(41)]
    d.line(pts, fill=fill, width=max(6, r // 7), joint="curve")
    for k in range(10):
        a = k * math.pi / 5; d.line((cx + r * .72 * math.cos(a), cy + r * .72 * math.sin(a), cx + r * math.cos(a), cy + r * math.sin(a)), fill=fill, width=max(6, r // 8))


def exclaim(d, x, y, s, fill):
    d.polygon([(x - s * .22, y), (x + s * .22, y), (x + s * .08, y + s * 1.1), (x - s * .08, y + s * 1.1)], fill=fill)
    d.ellipse((x - s * .13, y + s * 1.3, x + s * .13, y + s * 1.56), fill=fill)


def flower(d, cx, cy, r, petal, center):
    for k in range(7):
        a = k * 2 * math.pi / 7; px, py = cx + r * .62 * math.cos(a), cy + r * .62 * math.sin(a)
        d.ellipse((px - r * .42, py - r * .42, px + r * .42, py + r * .42), fill=petal, outline=(90, 70, 50, 120))
    d.ellipse((cx - r * .3, cy - r * .3, cx + r * .3, cy + r * .3), fill=center)


def doodle(canvas, kind, cx, cy, size, fill, rng, shadow=True, angle=None):
    """Hand-drawn sticker from the asset pack centred on (cx, cy); drawn fallback when the asset is absent."""
    sp = fx.sticker(kind, int(size), fill, rng.uniform(-16, 16) if angle is None else angle)
    if sp is not None:
        fx.place(canvas, sp, cx - sp.width // 2, cy - sp.height // 2, shadow=shadow, scale=0.55)
        return
    d = ImageDraw.Draw(canvas); r = int(size / 2.6); f = tuple(fill) + (255,) if len(fill) == 3 else fill
    if kind == "heart": heart(d, cx, cy, r, f)
    elif kind == "exclaim": exclaim(d, cx, cy - r, r * 1.1, f)
    elif kind == "sun": sun(d, cx, cy, int(r * 1.2), f)
    elif kind == "flower": flower(d, cx, cy, r, f, (230, 150, 60, 255))
    else: sparkle(d, cx, cy, r, f, (40, 40, 40, 160))


def stroke_text(d, xy, text, fnt, fill, stroke=(255, 255, 255), sw=0, anchor=None):
    d.text(xy, text, font=fnt, fill=fill, stroke_width=sw, stroke_fill=stroke, anchor=anchor)


def _dt(p):
    v = p.get("scheduled_at")
    return v if isinstance(v, datetime) else datetime.now(timezone.utc)


def _png(canvas):
    out = io.BytesIO(); canvas.convert("RGB").save(out, "PNG", optimize=True); return out.getvalue()


# ------------------------------------------------------------------ layout glue (slots come from the planner)
from .collage_layout_planner import plan_slots, junction_points, SAFE_Y0, SAFE_Y1, MARGIN_X
from .captions import pick_caption, FALLBACKS


def _plan(ctx, photos, **kw):
    """Planner call shared by all styles -> (photos in slot order, slots, plan). The plan is kept for the report."""
    plan = plan_slots(photos, **kw)
    ctx["plan"] = plan
    return plan["photos"], plan["slots"], plan


def _tile(p, s, inset=0, theme=None):
    """Photo cropped to the slot's ratio (minus `inset` px frame on every side), colour-graded for `theme`;
    records crop / contain on the slot."""
    tile, info = fit(p, (s["w"] - 2 * inset, s["h"] - 2 * inset), True)
    s["mode"] = info["mode"]
    return (fx.grade(tile, theme) if theme else tile), info


def _in_slot(cd, s):
    """(card, x, y): the (possibly rotated / torn) card, shrunk if needed so it never leaves its slot, centred in it."""
    if cd.width > s["w"] or cd.height > s["h"]:
        cd = cd.copy(); cd.thumbnail((s["w"], s["h"]), Image.Resampling.LANCZOS)
    return cd, s["x"] + (s["w"] - cd.width) // 2, s["y"] + (s["h"] - cd.height) // 2


def _fit_font(d, text, kind, size, max_w, min_size=28, max_lines=2, weight=None):
    """Largest font <= size whose wrapped text needs <= max_lines. Returns (font, lines)."""
    while True:
        f = font(kind, size, weight); lines = wrap(d, text, f, max_w, max_lines + 1)
        if len(lines) <= max_lines or size <= min_size:
            return f, lines[:max_lines]
        size -= 4


def _tape(canvas, cx, cy, w, h, angle, rng, kind="masking", tint=(236, 226, 186)):
    t = fx.tape_sprite(kind, w, h, angle, rng, tint)
    fx.place(canvas, t, cx - t.width // 2, cy - t.height // 2, contact=(2.5, (1, 3), .5), ambient=(7, (2, 5), .22))


# ------------------------------------------------------------------ 1. playful / hype
# Tiles touch (gap 0) on a dark background. Each tile's subject is cut out, scaled up a little and pasted OVER the
# neighbouring tiles with a ragged torn-paper white outline (fx.torn_die_cut). No cutout -> the whole tile gets the
# same torn white rim (never a plain white box). Hand-drawn stickers: some under the cutouts, some over them.
PH_MARGIN = 36                       # dark side margin; tiles touch each other, not the edge
PH_OUTLINE = (14, 18)                # torn white outline width (px), per cutout
PH_DOODLES = (("asterisk", (255, 214, 60)),      # yellow burst rays
              ("heart", (120, 235, 160)),        # green heart
              ("sun", (255, 238, 150)),          # spiral sun
              ("exclaim", (255, 140, 110)))      # orange "!" scribble


def _pop_limits(plan):
    """Where a pop-out cutout (outline included) may reach: whole width, safe zone top, a little into the caption band."""
    return (8, SAFE_Y0 + 4, W - 8, plan["caption_top"] + 40)


def _pop_out(s, bbox, k0, limits, ring, max_shift=40):
    """Scale factor k (<= k0) and shift (dx, dy) so the subject bbox (tile coords), scaled by k about its own centre,
    stays inside `limits` with room for the outline. The shift is capped, k shrinks until it fits."""
    L, T, R, B = limits
    gcx, gcy = s["x"] + (bbox[0] + bbox[2]) / 2, s["y"] + (bbox[1] + bbox[3]) / 2
    hw, hh = (bbox[2] - bbox[0]) / 2, (bbox[3] - bbox[1]) / 2
    k = k0
    while k > 1.0:
        ex, ey = hw * k + ring, hh * k + ring
        dx_lo, dx_hi = L + ex - gcx, R - ex - gcx
        dy_lo, dy_hi = T + ey - gcy, B - ey - gcy
        if dx_lo <= dx_hi and dy_lo <= dy_hi:
            dx, dy = min(max(0.0, dx_lo), dx_hi), min(max(0.0, dy_lo), dy_hi)
            if abs(dx) <= max_shift and abs(dy) <= max_shift:
                return k, dx, dy
        k -= 0.02
    return 1.0, 0.0, 0.0


def _seam_points(plan, limits):
    """Slot corners / edge midpoints = the seams where tiles meet (deduped to a 24 px grid)."""
    L, T, R, B = limits
    pts = {}
    for s in plan["slots"]:
        x0, y0, x1, y1 = s["x"], s["y"], s["x"] + s["w"], s["y"] + s["h"]
        for px, py in ((x0, y0), (x1, y0), (x0, y1), (x1, y1), ((x0 + x1) // 2, y0), ((x0 + x1) // 2, y1), (x0, (y0 + y1) // 2), (x1, (y0 + y1) // 2)):
            if 60 <= px <= W - 60 and T + 30 <= py <= plan["caption_top"] - 10:
                pts[(px // 24, py // 24)] = (int(px), int(py))
    return list(pts.values())


def playful_hype(photos, copy, rng, ctx):
    canvas = Image.new("RGBA", (W, H), (30, 30, 30, 255))
    photos, slots, plan = _plan(ctx, photos, caption=210, gap=0, margin_x=PH_MARGIN)
    plan["min_gap"] = 0                                            # tiles touch on purpose (validator reads this)
    limits = _pop_limits(plan)
    overlays = []                                                   # (z, sprite, x, y, subject bbox in canvas coords or None)
    for p, s in zip(photos, slots):
        s2 = dict(s); s2["w"] += 1; s2["h"] += 1                     # +1 px right/bottom: no hairline seam between tiles
        tile, info = _tile(p, s2, theme="playful_hype"); s["mode"] = s2["mode"]
        canvas.alpha_composite(tile.convert("RGBA"), (s["x"], s["y"]))
        outline = rng.randint(*PH_OUTLINE)
        mask = cut_mask(tile) if info["mode"] == "crop" else None   # no cutout on a contained (blurred-bg) tile
        bbox = mask.point(lambda a: 255 if a > 40 else 0).getbbox() if mask is not None else None
        res = None
        if bbox:                                                    # subject pops out of its tile, over the neighbours
            k, dx, dy = _pop_out(s, bbox, rng.uniform(1.12, 1.24), limits, outline * 1.45)
            cxt, cyt = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
            big = tile if k == 1.0 else tile.resize((round(tile.width * k), round(tile.height * k)), Image.Resampling.LANCZOS)
            bigm = mask if k == 1.0 else mask.resize(big.size, Image.Resampling.LANCZOS)
            res = fx.torn_die_cut(big, bigm, outline, rng)
            if res is not None:
                sprite, (ox, oy) = res
                gx = s["x"] + (bbox[0] + bbox[2]) / 2 + dx - k * cxt + ox
                gy = s["y"] + (bbox[1] + bbox[3]) / 2 + dy - k * cyt + oy
                half_w, half_h = (bbox[2] - bbox[0]) * k / 2, (bbox[3] - bbox[1]) * k / 2
                ccx, ccy = s["x"] + (bbox[0] + bbox[2]) / 2 + dx, s["y"] + (bbox[1] + bbox[3]) / 2 + dy
                sub_box = (ccx - half_w, ccy - half_h, ccx + half_w, ccy + half_h)
                overlays.append((s["w"] * s["h"], sprite, int(round(gx)), int(round(gy)), sub_box))
        if res is None:                                             # fallback: the WHOLE tile gets a torn white rim
            tm = fx.torn_mask(tile.size, rng, amp=6, wavelength=44, fibre=False)
            res = fx.torn_die_cut(tile, tm, outline, rng)
            if res is not None:
                sprite, (ox, oy) = res
                overlays.append((s["w"] * s["h"], sprite, s["x"] + ox, s["y"] + oy, None))
            else:                                                   # cannot happen (tm is never empty) - keep the plain tile
                log.warning("playful_hype: torn fallback produced nothing")
    overlays.sort(key=lambda o: o[0])                               # small tiles first, hero on top

    # ---- doodles: hand-drawn assets, each rotated; ~half UNDER the cutouts, ~half OVER them
    seams = _seam_points(plan, limits)
    cut_pts = []                                                    # points just OUTSIDE a cutout's edge: a doodle there is half under it
    for _z, _sp, _x, _y, bx in overlays:
        if bx:
            bw, bh = bx[2] - bx[0], bx[3] - bx[1]
            cut_pts += [(bx[0] - 26, bx[1] + bh * .22), (bx[0] - 26, bx[1] + bh * .62), (bx[2] + 26, bx[1] + bh * .3),
                        (bx[2] + 26, bx[1] + bh * .75), (bx[0] + bw * .3, bx[1] - 22), (bx[0] + bw * .75, bx[1] - 22)]
    cut_pts = [(int(x), int(y)) for x, y in cut_pts if 50 <= x <= W - 50 and limits[1] + 50 <= y <= plan["caption_top"] - 20]
    rng.shuffle(seams); rng.shuffle(cut_pts)
    n_d = min(8, max(5, len(seams) + len(cut_pts)))
    behind, front = [], []
    for k in range(n_d):
        kind, col = PH_DOODLES[k % 4]
        is_front = (k + k // 4) % 2 == 1 or not cut_pts            # no cutouts (fallback): nothing to hide behind
        pool = (seams if is_front else cut_pts) or seams or cut_pts   # under-doodles hug a cutout so it really covers part of them
        if not pool:
            continue
        cx, cy = pool.pop()
        cx += rng.randint(-14, 14); cy += rng.randint(-14, 14)
        item = (kind, cx, cy, rng.randint(104, 150), col, rng.choice((-1, 1)) * rng.uniform(8, 26))
        (front if is_front else behind).append(item)

    def _doodles(items):
        for kind, cx, cy, size, col, ang in items:
            doodle(canvas, kind, cx, cy, size, col, rng, shadow=True, angle=ang)
    _doodles(behind)
    for _z, sprite, x, y, _bx in overlays:
        fx.place(canvas, sprite, x, y, mode="normal", contact=(3, (2, 4), .5), ambient=(16, (5, 12), .45))
    _doodles(front)
    d = ImageDraw.Draw(canvas)
    text = str(copy.get("text", "")).upper()
    if text:
        f, lines = _fit_font(d, text, "marker", 74, W - 120, 44, 2)
        lh = int(f.size * 1.2); y = plan["caption_top"] + (210 - lh * len(lines)) // 2
        for ln in lines:
            stroke_text(d, (W // 2, y), ln, f, (255, 255, 255), (20, 20, 20), 6, anchor="ma"); y += lh
    return canvas


# ------------------------------------------------------------------ 2. k-pop editorial
# Vogue-cover collage look (B&W, gradient serif masthead behind the cut-out subject, small serif names list on the sides).
# All of it lives in kpop_editorial_fx.py; without a usable rembg cut-out the tile uses its own side-text fallback.
def kpop_editorial(photos, copy, rng, ctx):
    from . import kpop_editorial_fx as kp
    return kp.render(photos, copy, rng, ctx)


# ------------------------------------------------------------------ 3. photo dump
def _fit_label(d, text, kind, size, max_w, min_size=26, weight=None):
    """Largest font <= size whose rendering of `text` fits max_w."""
    while size > min_size:
        f = font(kind, size, weight)
        if d.textlength(text, font=f) <= max_w:
            return f
        size -= 2
    return font(kind, min_size, weight)


def photo_dump(photos, copy, rng, ctx):
    """Notebook page photo dump. All the look (grid, cream cards, dust, tape, ruled page, dog-ear) is in photo_dump_fx.py."""
    from . import photo_dump_fx as pd
    canvas = pd.build_page(rng)                                     # desk + ragged ruled page, holes, stain, dog-ear
    grid = pd.build_grid(len(photos), rng, caption=130)             # 3-3-2 style grid, every card the same size
    plan = pd.make_plan(photos, grid, caption=130); ctx["plan"] = plan
    used = set(); start = rng.randrange(len(FALLBACKS)); labels = []
    for k, (p, c, s) in enumerate(zip(photos, grid["cards"], plan["slots"])):
        tile, info, face = pd.photo_tile(p, c["photo"], _load, rng)  # face-aware crop, warm fade, dust + scratches
        s["mode"] = info["mode"]
        pad = 40                                                     # room round the card for tape that sticks out
        layer = pd.build_card(tile, c, rng, pad)                     # cream polaroid frame
        pd.add_tape(layer, c, pad, face, rng)                        # translucent tape, never on the face
        a = rng.uniform(0.5, 1.6) * rng.choice((-1, 1))
        card = layer.rotate(a, expand=True, resample=Image.Resampling.BICUBIC)
        cx, cy = c["x"] + c["w"] / 2, c["y"] + c["h"] / 2
        fx.place(canvas, card, int(cx - card.width / 2), int(cy - card.height / 2), contact=(3, (2, 4), .6), ambient=(16, (5, 11), .34))
        base = c["y"] + c["b"] + c["photo"] + round(c["strip"] * .62)    # ONE baseline for caption and date
        if k % 2 == 0:
            labels.append((cx, base, pick_caption(p.get("caption"), used, start + k), 1.0))
        else:
            labels.append((cx, base, _dt(p).strftime("%b %d, '%y").lower(), .82))
    d = ImageDraw.Draw(canvas); size = min(60, int(grid["strip"] * .6))
    for cx, base, text, sc in labels:
        f = pd.fit_hand(d, text, int(size * sc), grid["cw"] - 2 * grid["b"] - 8)
        d.text((cx, base), text, font=f, fill=pd.INK + (255,), anchor="ms")     # "ms" = middle / BASELINE
    plan["contain_slots"] = sum(1 for s in plan["slots"] if s.get("mode") == "contain")
    t = str(copy.get("text", "")).lower()
    if t:
        f, lines = _fit_font(d, t, "caveat", 66, W - 120, 40, 2, 700)
        lh = int(f.size * 1.1); y = plan["caption_top"] + (130 - lh * len(lines)) // 2
        for ln in lines:
            d.text((W // 2, y), ln, font=f, fill=pd.INK + (255,), anchor="ma"); y += lh
    return canvas


# ------------------------------------------------------------------ 4. streetwear / baddie
# Halftone B&W zine look on a noisy dark background. Everything lives in streetwear_fx.py (no other style touches it).
def streetwear_baddie(photos, copy, rng, ctx):
    from . import streetwear_fx as sw
    return sw.render(photos, copy, rng, ctx)


# ------------------------------------------------------------------ 5. classy editorial
def classy_editorial(photos, copy, rng, ctx):
    from . import classy_editorial_fx as ce                # bone page, hairline tiles, 8 px grid (see that file)
    return ce.render(photos, copy, rng, ctx)


# ------------------------------------------------------------------ 6. scrapbook chaos
def scrapbook_chaos(photos, copy, rng, ctx):
    """Dense, overlapping paper-scrap collage: irregular torn photos + rembg cut-outs, ransom-note title, PNG flowers /
    stickers, handwritten note, tape, kraft / pink / cream paper layers. All of it lives in scrapbook_chaos_fx.py."""
    from . import scrapbook_chaos_fx as sc
    return sc.render(photos, copy, rng, ctx)


STYLES = {"playful_hype": playful_hype, "kpop_editorial": kpop_editorial, "photo_dump": photo_dump,
          "streetwear_baddie": streetwear_baddie, "classy_editorial": classy_editorial, "scrapbook_chaos": scrapbook_chaos}


def render_style(photos, theme, copy, report=None):
    """PNG bytes. If `report` (a dict) is given, report["plan"] gets the slot plan (JSON-safe) for the validator."""
    rng = random.Random(str(theme) + str([p.get("asset_id") for p in photos]))
    ctx = {}
    canvas = STYLES[theme](photos, copy, rng, ctx)
    canvas = fx.grain(canvas, rng, GRAIN.get(theme, .06))                 # gaussian film / paper grain, last
    png = _png(canvas)
    if report is not None and ctx.get("plan"):
        report["plan"] = {k: v for k, v in ctx["plan"].items() if k != "photos"}
    return png
