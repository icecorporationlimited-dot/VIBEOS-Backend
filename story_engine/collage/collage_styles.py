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
         "classy_editorial": 0.045, "scrapbook_chaos": 0.06}                      # gaussian grain opacity per style


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
def playful_hype(photos, copy, rng, ctx):
    canvas = Image.new("RGBA", (W, H), (30, 30, 30, 255))
    photos, slots, plan = _plan(ctx, photos, caption=210)
    yellow, green, orange = (255, 214, 60), (120, 235, 160), (255, 140, 110)
    for p, s in zip(photos, slots):
        tile, info = _tile(p, s, theme="playful_hype")
        mask = cut_mask(tile) if info["mode"] == "crop" else None      # no die-cut on a contained (blurred-bg) tile
        if mask is not None:
            canvas.alpha_composite(die_cut(tile, mask), (s["x"], s["y"]))
        else:                                                           # fallback: torn white paper card inside the slot
            tile2, _i = _tile(p, s, inset=18, theme="playful_hype")
            cd, x, y = _in_slot(fx.torn_card(tile2, rng, border=18, amp=7, angle=rng.uniform(-2, 2)), s)
            fx.place(canvas, cd, x, y, mode="normal", contact=(3.5, (2, 4), .55), ambient=(20, (8, 16), .5))
    kinds = (("sparkle", yellow), ("heart", green), ("exclaim", orange), ("sun", yellow))
    for k, (cx, cy) in enumerate(junction_points(plan, rng, 8)):        # doodles sit in the gaps, not on faces
        kind, col = kinds[k % 4]
        doodle(canvas, kind, cx, cy, rng.randint(52, 76), col, rng, shadow=False)
    d = ImageDraw.Draw(canvas)
    text = str(copy.get("text", "")).upper()
    if text:
        f, lines = _fit_font(d, text, "marker", 74, W - 120, 44, 2)
        lh = int(f.size * 1.2); y = plan["caption_top"] + (210 - lh * len(lines)) // 2
        for ln in lines:
            stroke_text(d, (W // 2, y), ln, f, (255, 255, 255), (20, 20, 20), 6, anchor="ma"); y += lh
    return canvas


# ------------------------------------------------------------------ 2. k-pop editorial
def kpop_editorial(photos, copy, rng, ctx):
    canvas = fx.paper_bg((242, 242, 246), "fine", .6)
    photos, slots, plan = _plan(ctx, photos)
    purple, orchid = (123, 108, 255, 235), (205, 110, 255, 235)
    for k, (p, s) in enumerate(zip(photos, slots)):
        tw, th = s["w"], s["h"]
        tile, info = _tile(p, s, theme="kpop_editorial")              # grade = lavender-fade mono
        col = purple if k % 2 == 0 else orchid
        t = tile.convert("RGBA"); d = ImageDraw.Draw(t)
        narrow = tw < 600
        word = "VIBE" if narrow else "VIBEOS"; fs = int(tw * (.62 if narrow else .30)); f = font("bodoni_i", fs)
        while d.textlength(word, font=f) > tw * .86 and fs > 60: fs -= 8; f = font("bodoni_i", fs)
        mask = cut_mask(tile) if info["mode"] == "crop" else None
        layer = Image.new("RGBA", t.size, (0, 0, 0, 0)); ImageDraw.Draw(layer).text((tw // 2, 10), word, font=f, fill=col, anchor="ma")
        t.alpha_composite(layer)
        if mask is not None:                                       # subject re-pasted over the masthead = overlap look, inside its own slot
            t.paste(tile.convert("RGBA"), (0, 0), mask.filter(ImageFilter.GaussianBlur(.8)))
        if k == 0:                                                 # the headline lives on the hero tile only
            tf = font("playfair_i", min(64, int(th * .11)))
            lines = wrap(d, str(copy.get("text", "")).lower(), tf, tw - 60, 2)
            ImageDraw.Draw(t).text((30, th - 30 - len(lines) * int(tf.size * 1.15)), "\n".join(lines), font=tf, fill=col)
        ImageDraw.Draw(t).rectangle((0, 0, tw - 1, th - 1), outline=col, width=5)
        fx.place(canvas, t, s["x"], s["y"], contact=(2.5, (1, 3), .32), ambient=(16, (0, 9), .2))
    return canvas


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
def _halftone_panel(pw, ph, hero, rng, cell=10):
    """Dark halftone panel with a torn edge. Dots are drawn at 2x and downsampled (smooth dots, no jaggies)."""
    ss = fx.SS
    bg = hero.filter(ImageFilter.GaussianBlur(2)).point(lambda v: int(v * .55))
    small = bg.resize((max(1, pw // cell), max(1, ph // cell)), Image.Resampling.BOX); sp = small.tobytes()
    dots = Image.new("L", (pw * ss, ph * ss), 255); dd = ImageDraw.Draw(dots); c2 = cell * ss
    for gy in range(small.height):
        for gx in range(small.width):
            rr = (1 - sp[gy * small.width + gx] / 255) * c2 * .72
            cx, cy = gx * c2 + c2 / 2, gy * c2 + c2 / 2
            dd.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=0)
    dots = dots.resize((pw, ph), Image.Resampling.LANCZOS)
    panel = Image.merge("RGBA", (dots, dots, dots, Image.new("L", (pw, ph), 255)))
    panel.putalpha(fx.torn_mask((pw, ph), rng, amp=10, wavelength=90))
    return panel


def streetwear_baddie(photos, copy, rng, ctx):
    canvas = fx.paper_bg((230, 230, 224), "crumple")               # photocopy / zine paper
    d = ImageDraw.Draw(canvas); neon = (182, 255, 60, 255); ink = (14, 14, 14, 255)
    photos, slots, plan = _plan(ctx, photos, header=84, caption=215)       # slot 0 = hero (the planner makes it the biggest fit)
    issue = _dt(photos[0]).timetuple().tm_yday
    d.text((W // 2, SAFE_Y0 + 6), f"VIBEOS RAW / ISSUE #{issue:02d}", font=font("anton", 50), fill=ink, anchor="ma")
    bx0, by0 = min(s["x"] for s in slots) - 14, min(s["y"] for s in slots) - 14           # dark halftone panel behind the whole grid
    bx1, by1 = max(s["x"] + s["w"] for s in slots) + 14, max(s["y"] + s["h"] for s in slots) + 14
    pw, ph = bx1 - bx0, by1 - by0
    hero = ImageOps.grayscale(fx.grade(fit(photos[0], (pw, ph)), "streetwear_baddie"))
    canvas.alpha_composite(_halftone_panel(pw, ph, hero, rng), (bx0, by0))
    for k, (p, s) in enumerate(zip(photos, slots)):
        if k == 0:                                                         # hero: die-cut subject, else white-framed tile
            sub, sinfo = _tile(p, s, theme="streetwear_baddie")             # high-contrast B&W grade
            mask = cut_mask(sub) if sinfo["mode"] == "crop" else None
            if mask is not None:
                canvas.alpha_composite(die_cut(sub, mask, 12), (s["x"], s["y"]))
            else:
                t = sub.convert("RGBA"); ImageDraw.Draw(t).rectangle((0, 0, s["w"] - 1, s["h"] - 1), outline=(255, 255, 255, 255), width=10)
                canvas.alpha_composite(t, (s["x"], s["y"]))
        else:                                                              # torn B&W snippets
            im, _i = _tile(p, s, inset=10, theme="streetwear_baddie")
            cd, x, y = _in_slot(fx.torn_card(im, rng, border=14, amp=7, angle=rng.uniform(-2, 2)), s)
            fx.place(canvas, cd, x, y, contact=(3.5, (2, 4), .6), ambient=(20, (7, 15), .4))
            _tape(canvas, s["x"] + s["w"] // 2 + rng.randint(-30, 30), s["y"] + 18, 130, 34, rng.uniform(-10, 10), rng, "masking", (190, 255, 70))
    d = ImageDraw.Draw(canvas); s0 = slots[0]
    for text, kind, size, xy, fill in (("STYLE 01", "anton", min(58, int((s0["w"] - 40) / 4.6)), (s0["x"] + 14, s0["y"] + 14), (255, 255, 255, 255)),
                                       ("MUST HAVE!", "marker", min(40, int((s0["w"] - 40) / 7.0)), None, neon)):
        f = font(kind, size); wdt = d.textlength(text, font=f)
        if xy is None:
            xy = (s0["x"] + 14, s0["y"] + s0["h"] - size - 36)
        d.rectangle((xy[0] - 10, xy[1] - 6, xy[0] + wdt + 10, xy[1] + size + 14), fill=fill); d.text(xy, text, font=f, fill=ink)
    top = plan["caption_top"]
    d.text((MARGIN_X, top + 6), str(copy.get("text", "")).upper()[:36], font=font("bebas", 42), fill=ink)
    d.text((MARGIN_X, top + 52), "ZINE CULTURE", font=font("anton", 88), fill=ink)
    side_txt = "THE FIT IS READY  /  GRITTY LOOK  /  " + _dt(photos[0]).strftime("FALL '%y"); sf = font("bebas", 36)
    while d.textlength(side_txt, font=sf) > by1 - by0 and len(side_txt) > 8: side_txt = side_txt[:-2]
    side = Image.new("RGBA", (int(d.textlength(side_txt, font=sf)) + 4, 48), (0, 0, 0, 0)); ImageDraw.Draw(side).text((0, 0), side_txt, font=sf, fill=ink)
    side = side.rotate(-90, expand=True, resample=Image.Resampling.BICUBIC); canvas.alpha_composite(side, (W - 50, by0))
    if fx.sticker("asterisk", 8) is not None:                                # neon asterisks in the gaps (asset sprites only)
        for cx, cy in junction_points(plan, rng, 3):
            doodle(canvas, "asterisk", cx, cy, rng.randint(46, 62), neon[:3], rng, shadow=False)
    return canvas


# ------------------------------------------------------------------ 5. classy editorial
def classy_editorial(photos, copy, rng, ctx):
    canvas = fx.paper_bg((238, 236, 230), "fine"); d = ImageDraw.Draw(canvas); ink = (30, 28, 26, 255)
    photos, slots, plan = _plan(ctx, photos, header=70, caption=235)
    d.line((MARGIN_X, SAFE_Y0 + 8, W - MARGIN_X, SAFE_Y0 + 8), fill=ink, width=3)
    d.text((MARGIN_X, SAFE_Y0 + 22), "N° " + _dt(photos[0]).strftime("%d"), font=font("bodoni", 30), fill=ink)
    d.text((W - MARGIN_X, SAFE_Y0 + 22), _dt(photos[0]).strftime("%B %Y").upper(), font=font("bodoni", 30), fill=ink, anchor="ra")
    for p, s in zip(photos, slots):
        im, _i = _tile(p, s, inset=14, theme="classy_editorial")      # muted, slightly warm
        cd = fx.torn_card(im, rng, border=14, torn=False, paper=(250, 249, 245))
        fx.place(canvas, cd, s["x"], s["y"], contact=(3, (0, 3), .3), ambient=(20, (0, 11), .26))
    d = ImageDraw.Draw(canvas)
    t = str(copy.get("text", "")).capitalize(); top = plan["caption_top"]
    f, lines = _fit_font(d, t, "playfair_i", 70, W - 2 * MARGIN_X, 42, 2); y = top + 6
    for ln in lines: d.text((MARGIN_X, y), ln, font=f, fill=ink); y += int(f.size * 1.2)
    d.line((MARGIN_X, SAFE_Y1 - 40, W - MARGIN_X, SAFE_Y1 - 40), fill=ink, width=2); d.text((MARGIN_X, SAFE_Y1 - 34), "FOLK.", font=font("bodoni", 28), fill=ink)
    return canvas


# ------------------------------------------------------------------ 6. scrapbook chaos
_WASHI = (("washi_stripe", (255, 150, 180)), ("washi_dots", (140, 185, 255)), ("washi_stripe", (255, 214, 100)),
          ("masking", (236, 226, 186)), ("washi_dots", (170, 225, 170)), ("clear", None))


def scrapbook_chaos(photos, copy, rng, ctx):
    canvas = fx.paper_bg((243, 238, 228), "crumple", .8)
    photos, slots, plan = _plan(ctx, photos, caption=330)
    for p, s in zip(photos, slots):
        im, _i = _tile(p, s, inset=16, theme="scrapbook_chaos")
        cd, x, y = _in_slot(fx.torn_card(im, rng, border=16, amp=7, angle=rng.uniform(-3, 3)), s)
        fx.place(canvas, cd, x, y, contact=(3.5, (2, 4), .62), ambient=(22, (8, 17), .38))
        kind, tint = rng.choice(_WASHI)
        _tape(canvas, s["x"] + rng.randint(40, max(41, s["w"] - 40)), s["y"] + 18, 170, 40, rng.uniform(-14, 14), rng, kind, tint)
    pal = [(231, 140, 120), (118, 90, 150), (240, 196, 70), (86, 130, 110), (176, 80, 90), (120, 150, 200), (240, 170, 190)]
    cream = (252, 248, 238)
    top = plan["caption_top"]
    word = _dt(photos[0]).strftime("%b").lower() + " dump"; step = min(124, (W - 2 * MARGIN_X) // len(word)); tw = step - 8; th = int(tw * 1.17)
    x = (W - step * len(word)) // 2 + 4; prev = None
    for ch in word:                                                       # ransom-note letters: a different font on EVERY letter
        if ch != " ":
            fam = rng.choice([f_ for f_ in fx.SCRAP_LETTER_FONTS if f_ != prev]); prev = fam
            col = pal[rng.randrange(len(pal))]
            inverse = rng.random() < .28                                   # some letters are cream paper with a coloured glyph
            tile = fx.torn_tile((tw, th), rng, cream if inverse else col, amp=3)
            fnt = font(fam, int(tw * (.9 if fam in ("anton", "bebas") else .8)))
            ImageDraw.Draw(tile).text((tw // 2, th // 2 + 2), ch, font=fnt, fill=(col if inverse else cream) + (255,), anchor="mm")
            tile = tile.rotate(rng.uniform(-6, 6), expand=True, resample=Image.Resampling.BICUBIC)
            fx.place(canvas, tile, x - (tile.width - tw) // 2, top + 6 - (tile.height - th) // 2, contact=(2.5, (1, 3), .55), ambient=(9, (3, 6), .3))
        x += step
    t = str(copy.get("text", "")).lower()
    strip = fx.scrap((W - 2 * MARGIN_X, 124), rng)                         # torn paper strip from the asset pack
    sd = ImageDraw.Draw(strip); f, lines = _fit_font(sd, t, "caveat", 64, strip.width - 90, 34, 2, 700); lh = int(f.size * 1.05)
    y = (strip.height - lh * len(lines)) // 2 + lh // 2
    for ln in lines: sd.text((strip.width // 2, y), ln, font=f, fill=(40, 34, 28), anchor="mm"); y += lh
    strip = strip.rotate(-1.2, expand=True, resample=Image.Resampling.BICUBIC)
    fx.place(canvas, strip, (W - strip.width) // 2, top + 172, contact=(3, (2, 4), .55), ambient=(14, (4, 9), .3))
    for k, (cx, cy) in enumerate(junction_points(plan, rng, 6)):
        if k % 2 == 0: doodle(canvas, "flower", cx, cy, rng.randint(64, 86), rng.choice([(247, 226, 140), (245, 190, 200), (255, 255, 255)]), rng)
        else: doodle(canvas, "sparkle", cx, cy, rng.randint(46, 66), (255, 214, 80), rng)
    return canvas


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
