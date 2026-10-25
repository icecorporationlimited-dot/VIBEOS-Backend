"""Builds the collage asset pack in assets/collage/ (run once, commit the output; re-run to refresh).

    python tools/make_collage_assets.py

  papers/   paper_fine / crumple / ruled .jpg   greyscale paper textures (multiplied with a colour at render time)
  tapes/    masking / washi_stripe / washi_dots / clear .png   light RGBA strips (tinted at render time)
  stickers/ sparkle heart star flower exclaim sun scribble arrow squiggle smiley asterisk .png
            hand-drawn look: white fill + dark outline (render time multiplies the white by any colour)
  scraps/   scrap_1..4 / strip_1..2 .png   torn-paper scraps with fibre edges (caption strips, labels)

Everything is drawn at 4x and downsampled (anti-aliased). These are generated, procedural textures - to get real
scanned paper / photographed tape just drop your own files with the same names into the same folders; the renderer
only loads by name. Fonts are NOT made here: see tools/fetch_collage_fonts.py.
"""
import math
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from story_engine.collage import collage_fx as fx          # noqa: E402

OUT = ROOT / "assets" / "collage"
INK = (38, 34, 32, 255)
WHITE = (255, 255, 255, 255)
Q = 4                                                       # drawing supersample for sprites


def _save(im, rel, **kw):
    p = OUT / rel; p.parent.mkdir(parents=True, exist_ok=True); im.save(p, **kw)
    print("  ", rel, im.size, p.stat().st_size // 1024, "KB")


# ----------------------------------------------------------------------------- papers
def papers():
    for name, seed in (("fine", 11), ("crumple", 23), ("ruled", 37)):
        _save(fx.procedural_paper((720, 1280), seed, name), "papers/%s.jpg" % name, quality=84, optimize=True)


# ----------------------------------------------------------------------------- tapes
def _tape_alpha(w, h, rng, alpha):
    """Strip with short zig-zag torn ends (left / right)."""
    m = Image.new("L", (w * Q, h * Q), 0); d = ImageDraw.Draw(m)
    right = [(w * Q - rng.uniform(0, 7 * Q) * (1 if k % 2 else .25), h * Q * k / 8) for k in range(9)]
    left = [(rng.uniform(0, 7 * Q) * (1 if k % 2 else .25), h * Q * (8 - k) / 8) for k in range(9)]
    poly = [(0, 0), (w * Q, 0)] + right[1:-1] + [(w * Q, h * Q), (0, h * Q)] + left[1:-1]
    d.polygon(poly, fill=alpha)
    return m.resize((w, h), fx.LANCZOS)


def tapes():
    rng = random.Random(5); w, h = 480, 120
    # masking: warm cream, fine fibres, slightly translucent
    nr = np.random.default_rng(1)
    base = np.full((h, w), 238, np.float32) + nr.standard_normal((h, w)).astype(np.float32) * 4
    fib = Image.new("L", (w, h), 128); fd = ImageDraw.Draw(fib)
    for _ in range(260):
        x = rng.uniform(0, w); fd.line((x, 0, x + rng.uniform(-3, 3), h), fill=rng.choice((110, 150)), width=1)
    base += (np.asarray(fib.filter(ImageFilter.GaussianBlur(.6)), np.float32) - 128) * .18
    g = Image.fromarray(np.clip(base, 150, 255).astype(np.uint8), "L").convert("RGBA")
    g.putalpha(_tape_alpha(w, h, rng, 218)); _save(g, "tapes/masking.png", optimize=True)
    # washi stripe
    im = Image.new("RGBA", (w, h), (255, 255, 255, 255)); d = ImageDraw.Draw(im)
    for k in range(-h, w + h, 34):
        d.polygon([(k, 0), (k + 17, 0), (k + 17 - h, h), (k - h, h)], fill=(214, 214, 214, 255))
    im.putalpha(_tape_alpha(w, h, rng, 226)); _save(im, "tapes/washi_stripe.png", optimize=True)
    # washi dots
    im = Image.new("RGBA", (w, h), (255, 255, 255, 255)); d = ImageDraw.Draw(im)
    for yy in range(14, h, 28):
        for xx in range(14 + (yy // 28 % 2) * 14, w, 28):
            d.ellipse((xx - 5, yy - 5, xx + 5, yy + 5), fill=(205, 205, 205, 255))
    im.putalpha(_tape_alpha(w, h, rng, 226)); _save(im, "tapes/washi_dots.png", optimize=True)
    # clear tape: glassy, mostly transparent, two sheen streaks
    im = Image.new("RGBA", (w, h), (246, 246, 250, 255)); d = ImageDraw.Draw(im)
    for x0, wd in ((70, 26), (250, 14)):
        d.polygon([(x0, 0), (x0 + wd, 0), (x0 + wd - 30, h), (x0 - 30, h)], fill=(255, 255, 255, 255))
    im.putalpha(_tape_alpha(w, h, rng, 120)); _save(im, "tapes/clear.png", optimize=True)


# ----------------------------------------------------------------------------- stickers (hand-drawn look)
def _wobble(pts, rng, amp, closed=True, dens=6):
    """Densify a polyline and displace it with smooth noise so it looks drawn by hand."""
    out = []
    n = len(pts)
    for i in range(n if closed else n - 1):
        a, b = pts[i], pts[(i + 1) % n]
        seg = max(2, int(math.hypot(b[0] - a[0], b[1] - a[1]) / (dens * Q)))
        for k in range(seg):
            t = k / seg; out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    if not closed:
        out.append(pts[-1])
    m = len(out)
    nz = fx._periodic_fbm(max(m, 8), rng, max(8, m / 7), 2)
    nz2 = fx._periodic_fbm(max(m, 8), rng, max(8, m / 9), 2)
    return [(x + nz[i % len(nz)] * amp, y + nz2[i % len(nz2)] * amp) for i, (x, y) in enumerate(out)]


def _canvas(size=256):
    return Image.new("RGBA", (size * Q, size * Q), (0, 0, 0, 0))


def _finish(im, name, size=256):
    _save(im.resize((size, size), fx.LANCZOS), "stickers/%s.png" % name, optimize=True)


def _poly(d, pts, rng, fill=WHITE, ow=5, amp=2.2):
    w = _wobble(pts, rng, amp * Q)
    d.polygon(w, fill=fill); d.line(w + [w[0]], fill=INK, width=ow * Q, joint="curve")


def _star(cx, cy, r_out, r_in, n):
    return [(cx + (r_out if k % 2 == 0 else r_in) * math.cos(-math.pi / 2 + k * math.pi / n),
             cy + (r_out if k % 2 == 0 else r_in) * math.sin(-math.pi / 2 + k * math.pi / n)) for k in range(2 * n)]


def stickers():
    rng = random.Random(9); c = 128 * Q

    im = _canvas(); d = ImageDraw.Draw(im); _poly(d, _star(c, c, 116 * Q, 30 * Q, 4), rng); _finish(im, "sparkle")
    im = _canvas(); d = ImageDraw.Draw(im); _poly(d, _star(c, c + 6 * Q, 116 * Q, 50 * Q, 5), rng); _finish(im, "star")

    im = _canvas(); d = ImageDraw.Draw(im)
    pts = [(c + 100 * Q * 16 * math.sin(t) ** 3 / 16, c - 100 * Q * (13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)) / 16 + 6 * Q)
           for t in [i * 2 * math.pi / 60 for i in range(60)]]
    _poly(d, pts, rng, amp=1.6); _finish(im, "heart")

    im = _canvas(); d = ImageDraw.Draw(im)
    for k in range(7):
        a = k * 2 * math.pi / 7
        px, py = c + 62 * Q * math.cos(a), c + 62 * Q * math.sin(a)
        pts = [(px + 42 * Q * math.cos(t), py + 42 * Q * math.sin(t)) for t in [i * 2 * math.pi / 24 for i in range(24)]]
        _poly(d, pts, rng, ow=4, amp=1.2)
    pts = [(c + 34 * Q * math.cos(t), c + 34 * Q * math.sin(t)) for t in [i * 2 * math.pi / 24 for i in range(24)]]
    _poly(d, pts, rng, fill=(205, 205, 205, 255), ow=4, amp=1.0); _finish(im, "flower")

    im = _canvas(); d = ImageDraw.Draw(im)
    _poly(d, [(c - 30 * Q, 28 * Q), (c + 30 * Q, 28 * Q), (c + 14 * Q, 170 * Q), (c - 14 * Q, 170 * Q)], rng, ow=5, amp=1.6)
    _poly(d, [(c + 22 * Q * math.cos(t), 210 * Q + 22 * Q * math.sin(t)) for t in [i * 2 * math.pi / 20 for i in range(20)]], rng, ow=5, amp=1.0)
    _finish(im, "exclaim")

    im = _canvas(); d = ImageDraw.Draw(im)
    for k in range(10):                                                        # rays first, disc on top
        a = k * math.pi / 5; ca, sa = math.cos(a), math.sin(a)
        w = _wobble([(c + 82 * Q * ca, c + 82 * Q * sa), (c + 122 * Q * ca, c + 122 * Q * sa)], rng, 1.5 * Q, closed=False)
        d.line(w, fill=INK, width=17 * Q, joint="curve"); d.line(w, fill=WHITE, width=8 * Q, joint="curve")
    pts = [(c + 66 * Q * math.cos(t), c + 66 * Q * math.sin(t)) for t in [i * 2 * math.pi / 40 for i in range(40)]]
    _poly(d, pts, rng, amp=1.4); _finish(im, "sun")

    im = _canvas(); d = ImageDraw.Draw(im)                                    # scribble circle (outline only)
    pts = [(c + (96 + 5 * math.sin(t * 3)) * Q * math.cos(t * 1.0) * (1 + .03 * t), c + (82 + 4 * math.cos(t * 2)) * Q * math.sin(t))
           for t in [i * 2.35 * math.pi / 70 for i in range(70)]]
    w = _wobble(pts, rng, 2 * Q, closed=False)
    d.line(w, fill=INK, width=12 * Q, joint="curve"); d.line(w, fill=WHITE, width=5 * Q, joint="curve"); _finish(im, "scribble")

    im = _canvas(); d = ImageDraw.Draw(im)                                    # curved arrow
    curve = [(30 * Q + t * 190 * Q, 190 * Q - 120 * Q * math.sin(t * math.pi * .75) - 50 * Q * t) for t in [i / 30 for i in range(31)]]
    w = _wobble(curve, rng, 2 * Q, closed=False)
    d.line(w, fill=INK, width=17 * Q, joint="curve")
    ex, ey = w[-1]; ang = math.atan2(w[-1][1] - w[-4][1], w[-1][0] - w[-4][0])
    head = [(ex + 34 * Q * math.cos(ang), ey + 34 * Q * math.sin(ang)),
            (ex + 36 * Q * math.cos(ang + 2.5), ey + 36 * Q * math.sin(ang + 2.5)),
            (ex + 36 * Q * math.cos(ang - 2.5), ey + 36 * Q * math.sin(ang - 2.5))]
    d.polygon(head, fill=INK, outline=INK); d.line(w, fill=WHITE, width=8 * Q, joint="curve")
    d.polygon([((hx * .78 + ex * .22), (hy * .78 + ey * .22)) for hx, hy in head], fill=WHITE)
    _finish(im, "arrow")

    im = _canvas(); d = ImageDraw.Draw(im)                                    # squiggle
    sq = [(24 * Q + t * 208 * Q, c + 38 * Q * math.sin(t * math.pi * 4)) for t in [i / 80 for i in range(81)]]
    w = _wobble(sq, rng, 1.5 * Q, closed=False)
    d.line(w, fill=INK, width=19 * Q, joint="curve"); d.line(w, fill=WHITE, width=9 * Q, joint="curve"); _finish(im, "squiggle")

    im = _canvas(); d = ImageDraw.Draw(im)                                    # smiley
    pts = [(c + 108 * Q * math.cos(t), c + 108 * Q * math.sin(t)) for t in [i * 2 * math.pi / 48 for i in range(48)]]
    _poly(d, pts, rng, amp=1.6)
    for ex in (-36, 36):
        d.ellipse((c + ex * Q - 9 * Q, c - 38 * Q - 14 * Q, c + ex * Q + 9 * Q, c - 38 * Q + 14 * Q), fill=INK)
    sm = [(c + 58 * Q * math.cos(t), c + 6 * Q + 52 * Q * math.sin(t)) for t in [.25 * math.pi + i * .5 * math.pi / 14 for i in range(15)]]
    d.line(_wobble(sm, rng, 1.2 * Q, closed=False), fill=INK, width=9 * Q, joint="curve"); _finish(im, "smiley")

    im = _canvas(); d = ImageDraw.Draw(im)                                    # asterisk (6 spokes)
    for k in range(3):
        a = k * math.pi / 3 + .1
        p0 = (c - 108 * Q * math.cos(a), c - 108 * Q * math.sin(a)); p1 = (c + 108 * Q * math.cos(a), c + 108 * Q * math.sin(a))
        w = _wobble([p0, p1], rng, 1.4 * Q, closed=False); d.line(w, fill=INK, width=30 * Q, joint="curve")
    for k in range(3):
        a = k * math.pi / 3 + .1
        p0 = (c - 108 * Q * math.cos(a), c - 108 * Q * math.sin(a)); p1 = (c + 108 * Q * math.cos(a), c + 108 * Q * math.sin(a))
        d.line([p0, p1], fill=WHITE, width=20 * Q, joint="curve")
    _finish(im, "asterisk")


# ----------------------------------------------------------------------------- torn scraps
def scraps():
    specs = [("scrap_1", (900, 560), 7, 3), ("scrap_2", (900, 600), 9, 4), ("scrap_3", (860, 540), 6, 5), ("scrap_4", (900, 640), 8, 6),
             ("strip_1", (1000, 170), 6, 7), ("strip_2", (1000, 190), 7, 8)]
    for name, size, amp, seed in specs:
        rng = random.Random(seed)
        m = fx.torn_mask(size, rng, amp=amp, wavelength=90, fibre=True)
        paper = fx.paper_patch(size, rng, (252, 250, 244)).convert("RGBA")
        paper.putalpha(m); _save(paper, "scraps/%s.png" % name, optimize=True)


if __name__ == "__main__":
    print("writing", OUT)
    for fn in (papers, tapes, stickers, scraps):
        print(fn.__name__); fn()
