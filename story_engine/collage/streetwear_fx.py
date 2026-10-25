"""Streetwear / baddie = B&W halftone zine collage. Used ONLY by collage_styles.streetwear_baddie.

Look (reference: halftone B&W photos, torn paper, neon highlighter labels, vertical side text, scribble arrows)
  * background : noisy dark (film grain, mottling, dust, scratches, vignette)
  * photos     : high-contrast B&W -> HALFTONE dot screen (45 deg) -> torn paper card with masking tape
  * hero       : white STICKER-OUTLINE cut-out of the subject (rembg) pops out of its halftone card; no mask -> plain card
  * labels     : neon-green (#B6FF3C) highlighter strips ("FIT CHECK", "MUST HAVE!") - the ONLY place neon is used
  * heading    : Anton, big, with a solid box behind a few letters (stencil / ransom look)
  * side text  : Bebas, rotated 90 deg, in the dark side margins
  * scribbles  : hand-drawn white arrows (procedural, jittery double stroke)

Layout is still decided by collage_layout_planner (via collage_styles._plan): photos / labels / tape never leave their
slot, heading + caption live in the reserved bottom band, nothing important in the top / bottom 250 px.
Fonts: Anton / Bebas Neue / Permanent Marker come from assets/collage/fonts (tools/fetch_collage_fonts.py). If a file is
missing the Poppins fallback is squeezed horizontally so the heading still reads condensed.
"""
import logging
import math

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

from . import collage_fx as fx

log = logging.getLogger(__name__)

W, H = 1080, 1920
BG = 17                                   # base level of the dark noisy background
INK = (14, 14, 15)
PAPER = (238, 236, 228)
WHITE = (246, 246, 242)
NEON = (182, 255, 60)                     # #B6FF3C - labels only
TAPE = (228, 212, 164)                    # masking tape
HEADER, CAPTION = 64, 270                 # px reserved by the planner (top band / bottom band)
HEADINGS = ("STREET CODE", "ZINE CULTURE", "DROP SEASON", "BADDIE ERA")
LABELS = ("FIT CHECK", "MUST HAVE!", "NEW DROP")
BICUBIC, LANCZOS = Image.Resampling.BICUBIC, Image.Resampling.LANCZOS


# ================================================================== noisy dark background
def noisy_bg(rng):
    """Dark film-grain background: mottling + clumpy speckle + dust + scratches + vignette (RGBA, 1080x1920)."""
    nrng = fx.np_rng(rng)
    a = np.full((H, W), float(BG), np.float32)
    a += fx._smooth_noise((H, W), nrng, 240) * 2.4 + fx._smooth_noise((H, W), nrng, 70) * 1.5       # blotchy mottling
    a += nrng.standard_normal((H, W)).astype(np.float32) * 6.0                                          # film grain
    a += np.clip(fx._smooth_noise((H, W), nrng, 3) - 1.25, 0, None) * 15.0                              # clumpy speckle
    dust = Image.new("L", (W, H), 0); dd = ImageDraw.Draw(dust)
    for _ in range(320):                                                                                 # dust specks
        x, y, r = rng.uniform(0, W), rng.uniform(0, H), rng.choice((1, 1, 1, 2))
        dd.ellipse((x - r, y - r, x + r, y + r), fill=rng.randint(70, 200))
    for _ in range(16):                                                                                  # scratches
        x, y = rng.uniform(0, W), rng.uniform(0, H); ang = rng.uniform(-0.4, 0.4) + rng.choice((0, math.pi / 2)); ln = rng.uniform(40, 260)
        dd.line((x, y, x + ln * math.cos(ang), y + ln * math.sin(ang)), fill=rng.randint(40, 110), width=1)
    a += np.asarray(dust.filter(ImageFilter.GaussianBlur(0.6)), np.float32) * 0.17
    vx = ((np.arange(W, dtype=np.float32) - W / 2) / (W / 2)) ** 2
    vy = ((np.arange(H, dtype=np.float32) - H / 2) / (H / 2)) ** 2
    a *= 1.0 - 0.30 * np.clip(vy[:, None] + vx[None, :], 0, 1.6) / 1.6                                    # vignette
    a = np.clip(a, 0, 255)
    rgb = np.stack([a * 0.985, a, a * 1.04], axis=-1)
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB").convert("RGBA")


# ================================================================== halftone
def halftone(im, cell=6, angle=45.0, ink=INK, paper=PAPER, keep=0.10):
    """B&W photo -> halftone dot screen (AM screen, dots grow with darkness, anti-aliased). RGB in, RGB out, same size.
    `keep` mixes a little of the plain grey photo back in so faces stay readable."""
    g = ImageOps.autocontrast(ImageOps.grayscale(im.convert("RGB")), cutoff=1)
    soft = g.filter(ImageFilter.GaussianBlur(cell * 0.30))
    a = np.asarray(soft, np.float32) / 255.0
    a = np.clip((a - 0.5) * 1.18 + 0.5, 0, 1)                                                             # a touch more punch
    h, w = a.shape
    t = math.radians(angle); c, s = math.cos(t), math.sin(t); k = 2 * math.pi / cell
    xx = np.arange(w, dtype=np.float32)[None, :]; yy = np.arange(h, dtype=np.float32)[:, None]
    u = (xx * c + yy * s) * k; v = (yy * c - xx * s) * k
    screen = (np.cos(u) + np.cos(v)) * 0.25 + 0.5                                                          # 1 at dot centre, 0 at corners
    dot = np.clip((screen - a) * (cell * 0.55) + 0.5, 0, 1)                                                 # 1 = ink
    grey = np.asarray(g, np.float32) / 255.0
    ink_amt = np.clip(dot * (1 - keep) + (1 - grey) * keep, 0, 1)[..., None]
    out = np.asarray(paper, np.float32) * (1 - ink_amt) + np.asarray(ink, np.float32) * ink_amt
    res = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB")
    return res.filter(ImageFilter.GaussianBlur(0.45))                                                      # ink spread


# ================================================================== sticker cut-out (white outline, transparent outside)
def sticker_cutout(tile, mask, outline=14):
    """Subject on transparent background with a smooth white sticker outline (outline built at 2x, anti-aliased)."""
    tile = tile.convert("RGB"); w, h = tile.size
    m = mask.convert("L").filter(ImageFilter.GaussianBlur(0.8))
    ring = fx.grow(m.resize((w * fx.SS, h * fx.SS), BICUBIC), outline * fx.SS).resize((w, h), LANCZOS)
    ring = ImageChops.lighter(ring, m)
    out = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    out.paste(Image.new("RGBA", (w, h), WHITE + (255,)), (0, 0), ring)
    out.paste(tile.convert("RGBA"), (0, 0), m)
    return out


# ================================================================== small drawing helpers
def _squeeze(key):
    """1.0 with the real font, 0.76 for the Poppins fallback (keeps the condensed street look)."""
    return 1.0 if fx.has_font(key) else 0.76


def _shadow_kw(strong=1.0):
    return dict(mode="normal", contact=(3.5, (2, 4), min(1.0, .70 * strong)), ambient=(22, (8, 16), min(1.0, .55 * strong)))


def _tape(canvas, cx, cy, w, h, angle, rng):
    t = fx.tape_sprite("masking", w, h, angle, rng, TAPE)
    fx.place(canvas, t, int(cx - t.width // 2), int(cy - t.height // 2), mode="normal", contact=(2.5, (1, 3), .40), ambient=(7, (2, 5), .20))


def _clamp(sp, x, y, s, pad=0):
    """Top-left (x, y) moved so the sprite lies inside slot s (decoration never leaves its slot)."""
    x = min(max(x, s["x"] + pad), s["x"] + s["w"] - sp.width - pad)
    y = min(max(y, s["y"] + pad), s["y"] + s["h"] - sp.height - pad)
    return int(x), int(y)


# ================================================================== neon highlighter label
def highlighter_label(text, size, rng, angle=0.0):
    """Neon-green marker strip with ragged ends and black marker text -> RGBA sprite (rotated by `angle`)."""
    ss = 2; fnt = fx.family_font("marker", size * ss)
    tw = fnt.getlength(text); asc, desc = fnt.getmetrics()
    px, py = int(size * 0.42 * ss), int(size * 0.20 * ss)
    w, h = int(tw + 2 * px), int(asc + desc + 2 * py)
    j = lambda v: rng.uniform(-v, v) * ss
    top = [(0 + j(3), 0 + j(2))] + [(w * t, j(2.2)) for t in (0.25, 0.5, 0.75)] + [(w + j(5), 0 + j(2))]
    right = [(w + j(7), h * 0.5)]
    bot = [(w + j(5), h + j(2))] + [(w * t, h + j(2.2)) for t in (0.75, 0.5, 0.25)] + [(0 + j(3), h + j(2))]
    left = [(0 - j(7) - 3 * ss, h * 0.5)]
    bar = Image.new("RGBA", (w + 24 * ss, h + 12 * ss), (0, 0, 0, 0)); d = ImageDraw.Draw(bar)
    ox, oy = 10 * ss, 5 * ss
    d.polygon([(x + ox, y + oy) for x, y in top + right + bot + left], fill=NEON + (250,))
    for _ in range(4):                                                                                    # marker streaks
        yy = oy + rng.uniform(0.15, 0.85) * h
        d.line((ox + rng.uniform(0, w * .2), yy, ox + rng.uniform(w * .6, w), yy), fill=(168, 243, 50, 255), width=max(1, ss))
    d.text((ox + px + j(1), oy + py * 0.55), text, font=fnt, fill=INK + (255,))
    bar = bar.resize((bar.width // ss, bar.height // ss), LANCZOS)
    return bar.rotate(angle, expand=True, resample=BICUBIC) if angle else bar


def fit_label(text, s, rng, angle, max_frac=0.78, size=52):
    """Largest label (<= `size` px font) that is at most max_frac of the slot width."""
    while True:
        sp = highlighter_label(text, size, rng, angle)
        if sp.width <= s["w"] * max_frac or size <= 22:
            return sp
        size -= 4


# ================================================================== stencil heading (box behind some letters)
def heading(text, rng, max_w, max_cap=136, angle=-1.2):
    """Big condensed heading; a white box with a black letter sits behind 1-2 letters of every word. RGBA sprite."""
    key = "anton"; sq = _squeeze(key); ss = 2
    size = 230
    while True:
        f1 = fx.family_font(key, size); track = max(3.0, size * 0.035)
        adv = [f1.getlength(ch) * sq + track for ch in text]
        bb = f1.getbbox("H"); cap = bb[3] - bb[1]
        padx = max(8, size * 0.075)
        total = sum(adv) - track + 2 * padx
        if (total <= max_w and cap <= max_cap) or size <= 70:
            break
        size -= 6
    pady = max(6, size * 0.055); f2 = fx.family_font(key, size * ss)
    sw, sh = int(total + 24) * ss, int(cap + 2 * pady + 24) * ss
    sheet = Image.new("RGBA", (sw, sh), (0, 0, 0, 0)); d = ImageDraw.Draw(sheet)
    base = int((12 + pady + cap) * ss)                                                                   # baseline y at 2x
    boxed, i = set(), 0
    for word in text.split(" "):
        idx = [i + k for k in range(len(word))]
        n = 1 if len(word) <= 6 else 2
        for _ in range(8):
            pick = rng.sample(idx, min(n, len(idx)))
            if all(abs(a - b) > 1 for a in pick for b in pick if a != b):
                boxed.update(pick); break
        i += len(word) + 1
    x = (12 + padx) * ss
    for k, ch in enumerate(text):
        a2 = adv[k] * ss
        if ch != " ":
            gw = int(f2.getlength(ch)) + 8 * ss
            layer = Image.new("L", (gw, sh), 0)
            ImageDraw.Draw(layer).text((4 * ss, base), ch, font=f2, fill=255, anchor="ls")
            if sq != 1.0:
                layer = layer.resize((max(1, int(gw * sq)), sh), LANCZOS)
            gx = int(x - 4 * ss * sq)
            if k in boxed:
                cx, cy = x + (a2 - track * ss) / 2, base - cap * ss / 2
                bw, bh = (a2 - track * ss) / 2 + padx * ss * 0.9, cap * ss / 2 + pady * ss
                ang = math.radians(rng.uniform(-3.2, 3.2)); ca, sa = math.cos(ang), math.sin(ang)
                jx, jy = rng.uniform(-3, 3) * ss, rng.uniform(-3, 3) * ss
                pts = [(cx + jx + dx * ca - dy * sa, cy + jy + dx * sa + dy * ca) for dx, dy in ((-bw, -bh), (bw, -bh), (bw, bh), (-bw, bh))]
                d.polygon(pts, fill=WHITE + (255,))
                sheet.paste(Image.new("RGBA", layer.size, INK + (255,)), (gx, 0), layer)
            else:
                sheet.paste(Image.new("RGBA", layer.size, WHITE + (255,)), (gx, 0), layer)
        x += a2
    sp = sheet.resize((sheet.width // ss, sheet.height // ss), LANCZOS)
    return sp.rotate(angle, expand=True, resample=BICUBIC) if angle else sp


# ================================================================== condensed text helper + vertical side text
def cond_sprite(text, key, size, fill, track=0.0):
    """Tight RGBA text sprite; the Poppins fallback is squeezed so it looks condensed like Anton / Bebas."""
    f = fx.family_font(key, size); sq = _squeeze(key); asc, desc = f.getmetrics()
    col = tuple(fill) if len(fill) == 4 else tuple(fill) + (255,)
    w = int(sum(f.getlength(ch) + track for ch in text) if track else f.getlength(text)) + 10
    lay = Image.new("RGBA", (w, asc + desc + 8), (0, 0, 0, 0)); d = ImageDraw.Draw(lay)
    if track:
        x = 2.0
        for ch in text:
            d.text((x, 2), ch, font=f, fill=col); x += f.getlength(ch) + track
    else:
        d.text((2, 2), text, font=f, fill=col)
    if sq != 1.0:
        lay = lay.resize((max(1, int(lay.width * sq)), lay.height), LANCZOS)
    return lay.crop(lay.getbbox() or (0, 0, 1, 1))


def fit_cond(text, key, size, max_w, fill, min_size=26, track=0.0):
    """cond_sprite shrunk until it is at most max_w wide."""
    while True:
        sp = cond_sprite(text, key, size, fill, track)
        if sp.width <= max_w or size <= min_size:
            return sp
        size -= 2


def side_text(canvas, text, cx, y0, y1, rot, size=30, fill=(236, 236, 230, 235), track=5):
    """Bebas text rotated 90 deg (rot=90 reads bottom->top, rot=-90 top->bottom), centred on x=cx between y0..y1."""
    words = text.split("  /  ")
    while True:
        sp = cond_sprite("  /  ".join(words), "bebas", size, fill, track)
        if sp.width <= (y1 - y0) or len(words) <= 1:
            break
        words.pop()
    sp = sp.rotate(rot, expand=True, resample=BICUBIC)
    fx.blit(canvas, sp, cx - sp.width // 2, (y0 + y1) // 2 - sp.height // 2)


# ================================================================== scribble arrow
def scribble_arrow(canvas, p0, p1, rng, width=6, bend=0.30, color=WHITE):
    """Hand-drawn white arrow p0 -> p1: curved, jittery, drawn twice (second pass shorter) + two-stroke arrowhead."""
    ss = 2
    (x0, y0), (x1, y1) = p0, p1
    pad = 70
    bx0, by0 = int(max(0, min(x0, x1) - pad)), int(max(0, min(y0, y1) - pad))
    bx1, by1 = int(min(W, max(x0, x1) + pad)), int(min(H, max(y0, y1) + pad))
    lw, lh = (bx1 - bx0) * ss, (by1 - by0) * ss
    layer = Image.new("L", (lw, lh), 0); d = ImageDraw.Draw(layer)
    L = math.hypot(x1 - x0, y1 - y0) or 1.0
    nx, ny = -(y1 - y0) / L, (x1 - x0) / L; sgn = rng.choice((-1, 1))
    cxp, cyp = (x0 + x1) / 2 + nx * bend * L * sgn, (y0 + y1) / 2 + ny * bend * L * sgn

    def stroke(pts, w_, a=255):
        P = [((px - bx0) * ss, (py - by0) * ss) for px, py in pts]
        r = w_ * ss / 2
        for (ax, ay), (bx, by) in zip(P, P[1:]):
            d.line((ax, ay, bx, by), fill=a, width=int(w_ * ss))
        for ax, ay in P:
            d.ellipse((ax - r, ay - r, ax + r, ay + r), fill=a)

    def curve(t0, t1, jit, n=48):
        jx = fx._periodic_fbm(n, rng, 9, 2) * jit; jy = fx._periodic_fbm(n, rng, 9, 2) * jit
        out = []
        for k in range(n):
            t = t0 + (t1 - t0) * k / (n - 1)
            out.append(((1 - t) ** 2 * x0 + 2 * (1 - t) * t * cxp + t * t * x1 + jx[k],
                        (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cyp + t * t * y1 + jy[k]))
        return out
    main = curve(0.0, 1.0, 2.6); stroke(main, width)
    stroke(curve(0.04, 0.93, 3.4), max(2, width - 2), 200)                                               # second, looser pass
    ex, ey = main[-1]; px_, py_ = main[-4]; tang = math.atan2(ey - py_, ex - px_)
    for sgn2 in (-1, 1):                                                                                 # arrowhead wings
        a = tang + math.pi + sgn2 * math.radians(rng.uniform(24, 34)); ln = rng.uniform(34, 46)
        mx, my = ex + ln * .5 * math.cos(a) + rng.uniform(-2, 2), ey + ln * .5 * math.sin(a) + rng.uniform(-2, 2)
        stroke([(ex, ey), (mx, my), (ex + ln * math.cos(a), ey + ln * math.sin(a))], max(3, width - 1))
    nz = fx.gauss_L(layer.size, fx.np_rng(rng), 22, 226)                                                  # dry-marker texture
    layer = ImageChops.multiply(layer, nz).resize((bx1 - bx0, by1 - by0), LANCZOS)
    sp = Image.new("RGBA", layer.size, tuple(color) + (255,)); sp.putalpha(layer)
    fx.blit(canvas, sp, bx0, by0)


# ================================================================== torn paper caption strip
def caption_strip(text, rng, w, h=96):
    """Torn paper strip (asset scrap) with the copy line in black condensed type (1 line, else 2 smaller lines)."""
    strip = fx.scrap((w, h), rng, PAPER)
    t = str(text).upper()[:60]; max_w = w - 120
    sp = fit_cond(t, "bebas", 58, max_w, INK + (255,), min_size=40)
    lines = [sp]
    if sp.width > max_w:                                                  # still too long: two lines split at the middle space
        words = t.split(); cut = max(1, len(words) // 2)
        lines = [fit_cond(" ".join(words[:cut]), "bebas", 44, max_w, INK + (255,)), fit_cond(" ".join(words[cut:]) or " ", "bebas", 44, max_w, INK + (255,))]
    gap = 4; total = sum(l.height for l in lines) + gap * (len(lines) - 1); y = (h - total) // 2
    for l in lines:
        strip.alpha_composite(l, ((w - l.width) // 2, y)); y += l.height + gap
    return strip.rotate(1.4, expand=True, resample=BICUBIC)


# ================================================================== the style
def render(photos, copy, rng, ctx):
    """Full 1080x1920 streetwear/baddie canvas (RGBA). `photos` = selector dicts, best first."""
    from . import collage_styles as cs                         # late import: collage_styles imports this module lazily too
    canvas = noisy_bg(rng)
    photos, slots, plan = cs._plan(ctx, photos, header=HEADER, caption=CAPTION)
    n = len(slots)
    gy0 = min(s["y"] for s in slots); gy1 = max(s["y"] + s["h"] for s in slots)
    issue = cs._dt(photos[0]).timetuple().tm_yday
    hdr = cond_sprite("VIBEOS RAW  /  ISSUE #%02d" % issue, "anton", 42, (226, 226, 220), track=3)
    canvas.alpha_composite(hdr, ((W - hdr.width) // 2, cs.SAFE_Y0 + 6))

    # ---- vertical side text (rotated 90 deg), in the dark side margins
    stamp = cs._dt(photos[0]).strftime("FALL '%y")
    side_text(canvas, "THE FIT IS READY  /  GRITTY LOOK  /  " + stamp, W - 30, gy0, gy1, -90)
    side_text(canvas, "STREET ARCHIVE  /  NO FILTER  /  VOL. %02d" % (issue % 100), 30, gy0, gy1, 90)

    # ---- photos: halftone torn cards (+ sticker cut-out on the hero)
    placed = []                                                                                         # (x, y, w, h) of every card
    for k, (p, s) in enumerate(zip(photos, slots)):
        hero = k == 0
        if hero:
            tile, info = cs._tile(p, s, theme="streetwear_baddie")                                       # full slot, B&W graded
            mask = cs.cut_mask(tile) if info["mode"] == "crop" else None
            ht = halftone(tile, cell=7)
            if mask is not None:                                                                         # card inset + sticker pops out of it
                ins, bd = 34, 14
                card = fx.torn_card(ht.crop((ins, ins, s["w"] - ins, s["h"] - ins)), rng, border=bd, amp=8, angle=0, paper=PAPER)
                cx, cy = s["x"] + ins - bd, s["y"] + ins - bd
                fx.place(canvas, card, cx, cy, **_shadow_kw())
                stk = sticker_cutout(ht, mask, 14)
                fx.place(canvas, stk, s["x"], s["y"], mode="normal", contact=(3, (1, 3), .55), ambient=(16, (5, 10), .45))
                placed.append((cx, cy, card.width, card.height))
                _tape(canvas, cx + 26, cy + 20, 150, 40, -38, rng); _tape(canvas, cx + card.width - 26, cy + card.height - 20, 150, 40, -38, rng)
                continue
            im = ht.crop((26, 26, s["w"] - 26, s["h"] - 26))                                              # no cut-out: plain torn card
        else:
            tile, _i = cs._tile(p, s, inset=26, theme="streetwear_baddie")
            im = halftone(tile, cell=6)
        card = fx.torn_card(im, rng, border=14, amp=8, angle=rng.uniform(-2.6, 2.6), paper=PAPER)
        card, x, y = cs._in_slot(card, s)
        fx.place(canvas, card, x, y, **_shadow_kw())
        placed.append((x, y, card.width, card.height))
        if rng.random() < 0.5:                                                                           # tape: two corners or one on top
            _tape(canvas, x + 24, y + 20, 140, 38, -40, rng); _tape(canvas, x + card.width - 24, y + 20, 140, 38, 40, rng)
        else:
            _tape(canvas, x + card.width // 2 + rng.randint(-40, 40), y + 14, 150, 40, rng.uniform(-6, 6), rng)

    # ---- neon highlighter labels (the only neon on the canvas)
    s0 = slots[0]; s1 = slots[1] if n > 1 else slots[0]
    for text, s, corner, ang in ((LABELS[0], s0, "tl", -4.0), (LABELS[1], s1, "br", 3.0)) + (((LABELS[2], slots[2], "bl", -2.5),) if n >= 4 else ()):
        sp = fit_label(text, s, rng, ang)
        x = s["x"] + 12 if corner in ("tl", "bl") else s["x"] + s["w"] - sp.width - 12
        y = s["y"] + 16 if corner == "tl" else s["y"] + s["h"] - sp.height - 16
        x, y = _clamp(sp, x, y, s)
        fx.place(canvas, sp, x, y, mode="normal", contact=(2.5, (1, 3), .45), ambient=(8, (2, 5), .22))

    # ---- scribble arrows in the gap crossings
    top = plan["caption_top"]
    for cx, cy in [q for q in cs.junction_points(plan, rng) if q[1] < top - 80 and 110 <= q[0] <= W - 110][:2]:                  # never into the heading band
        ln = rng.randint(130, 180); ang = rng.uniform(-2.4, -0.7)                                      # up-ish diagonals
        scribble_arrow(canvas, (cx - ln * math.cos(ang) / 2, cy - ln * math.sin(ang) / 2),
                       (cx + ln * math.cos(ang) / 2, cy + ln * math.sin(ang) / 2), rng, width=9)

    # ---- heading band: stencil heading + torn caption strip
    hd = heading(rng.choice(HEADINGS), rng, W - 2 * cs.MARGIN_X)
    canvas.alpha_composite(hd, (cs.MARGIN_X - 6, top + 2))
    t = str(copy.get("text", "")).strip()
    if t:
        strip = caption_strip(t, rng, min(W - 2 * cs.MARGIN_X - 80, 760))
        sx, sy = cs.MARGIN_X + 20, min(top + hd.height + 2, cs.SAFE_Y1 - strip.height)
        fx.place(canvas, strip, sx, sy, mode="normal", contact=(3, (2, 4), .55), ambient=(14, (4, 9), .35))
        _tape(canvas, sx + 30, sy + 14, 100, 32, -35, rng)
        scribble_arrow(canvas, (W - 62, sy + 14), (sx + strip.width + 22, sy + strip.height // 2 + 6), rng, width=9, bend=0.35)   # "read this"
    return canvas
