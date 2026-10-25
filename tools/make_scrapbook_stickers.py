"""Builds the coloured flower / leaf sticker PNGs used by scrapbook_chaos (assets/collage/stickers/flower_*.png, leaf_sprig.png).

    python tools/make_scrapbook_stickers.py [out_dir]      # default: assets/collage/stickers

Hand-drawn look: wobbly petals, dark brown ink outline, flat colour + a lighter highlight. Drawn at 2x and downsampled
(anti-aliased). 400x400 RGBA, transparent background; the white die-cut sticker border is added at render time.
"""
import math
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 800                      # work size (2x)
OUT = 400
INK = (58, 40, 30, 255)


def _wob(rng, a=0.05):
    return 1 + rng.uniform(-a, a)


def petal(d, cx, cy, ang, length, width, fill, rng, ink=INK, lw=7, base=0.0, tip="round"):
    """One petal: a lens / teardrop polygon starting `base` px from the centre, pointing at angle `ang`."""
    pts = []
    n = 26
    for k in range(n + 1):                                   # outline: out along one side, back along the other
        t = k / n
        w = math.sin(t * math.pi) ** (0.8 if tip == "round" else 1.3) * width / 2 * _wob(rng, .06)
        pts.append((base + t * length * _wob(rng, .015), w))
    pts += [(x, -y) for x, y in reversed(pts)]
    ca, sa = math.cos(ang), math.sin(ang)
    poly = [(cx + x * ca - y * sa, cy + x * sa + y * ca) for x, y in pts]
    d.polygon(poly, fill=fill, outline=ink, width=lw)
    mid = [(cx + (base + length * .22) * ca, cy + (base + length * .22) * sa), (cx + (base + length * .78) * ca, cy + (base + length * .78) * sa)]
    d.line(mid, fill=tuple(min(255, c + 38) for c in fill[:3]) + (255,), width=max(3, lw - 1))     # lighter vein / highlight


def centre(d, cx, cy, r, fill, rng, dots=None, ink=INK):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill, outline=ink, width=7)
    if dots:
        for _ in range(dots):
            a, rr = rng.uniform(0, 6.28), rng.uniform(0, r * .68)
            x, y = cx + rr * math.cos(a), cy + rr * math.sin(a)
            d.ellipse((x - 5, y - 5, x + 5, y + 5), fill=INK)


def daisy(rng):
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(im); c = S // 2
    n = 13
    for k in range(n):
        petal(d, c, c, k * 2 * math.pi / n + rng.uniform(-.05, .05), S * .40, S * .15, (255, 253, 245, 255), rng, base=S * .06)
    centre(d, c, c, S * .115, (250, 196, 40, 255), rng, dots=9)
    return im


def blossom(rng, fill=(247, 156, 184, 255), mid=(255, 224, 120, 255)):
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(im); c = S // 2
    for k in range(5):
        petal(d, c, c, -math.pi / 2 + k * 2 * math.pi / 5, S * .40, S * .33, fill, rng, base=S * .02)
    centre(d, c, c, S * .09, mid, rng, dots=7)
    return im


def sunflower(rng):
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(im); c = S // 2
    for ring, (n, ln, wd, off) in enumerate(((16, .37, .13, 0), (16, .31, .12, math.pi / 16))):
        for k in range(n):
            petal(d, c, c, k * 2 * math.pi / n + off, S * ln, S * wd, (255, 200 - ring * 22, 30, 255), rng, base=S * .10, tip="pointed", lw=6)
    centre(d, c, c, S * .17, (120, 76, 40, 255), rng, dots=26)
    return im


def cosmos(rng):
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(im); c = S // 2
    for k in range(8):
        petal(d, c, c, k * 2 * math.pi / 8 + .2, S * .41, S * .24, (231, 84, 62, 255), rng, base=S * .03)
    centre(d, c, c, S * .075, (250, 205, 70, 255), rng, dots=5)
    return im


def tulip(rng):
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    green = (96, 160, 92, 255)
    d.line([(S * .5, S * .98), (S * .5, S * .52)], fill=INK, width=24); d.line([(S * .5, S * .98), (S * .5, S * .52)], fill=green, width=12)
    for sgn in (-1, 1):                                       # two leaves
        pts = [(S * .5, S * .95), (S * .5 + sgn * S * .30, S * .66), (S * .5 + sgn * S * .20, S * .93)]
        d.polygon(pts, fill=green, outline=INK, width=7)
    red = (232, 70, 96, 255)
    d.polygon([(S * .22, S * .20), (S * .36, S * .36), (S * .5, S * .10), (S * .64, S * .36), (S * .78, S * .20), (S * .80, S * .48),
               (S * .66, S * .66), (S * .34, S * .66), (S * .20, S * .48)], fill=red, outline=INK, width=8)
    d.line([(S * .36, S * .36), (S * .40, S * .62)], fill=(255, 150, 160, 255), width=8)
    d.line([(S * .64, S * .36), (S * .60, S * .62)], fill=(180, 40, 70, 255), width=8)
    return im


def sprig(rng):
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    stem = [(S * .22, S * .94), (S * .38, S * .62), (S * .56, S * .34), (S * .80, S * .08)]
    d.line(stem, fill=INK, width=22, joint="curve"); d.line(stem, fill=(84, 142, 84, 255), width=10, joint="curve")
    for i, (px, py) in enumerate(((.30, .78), (.38, .62), (.46, .48), (.58, .33), (.70, .19))):
        for sgn in (-1, 1):
            ang = -math.pi / 2.6 + sgn * (math.pi / 2.7) + rng.uniform(-.1, .1)
            petal(d, S * px, S * py, ang, S * (.21 - .012 * i), S * .12, (110 + i * 10, 170 + i * 4, 98, 255), rng, tip="pointed", lw=6)
    return im


def finish(im, path):
    im = im.resize((OUT, OUT), Image.Resampling.LANCZOS)
    bbox = im.getchannel("A").getbbox()
    if bbox:
        im = im.crop(bbox)
        side = max(im.size) + 8
        sq = Image.new("RGBA", (side, side), (0, 0, 0, 0)); sq.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
        im = sq
    im.save(path, "PNG", optimize=True)


def main(out_dir):
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(7)
    jobs = {"flower_daisy": daisy(rng), "flower_blossom": blossom(rng), "flower_blossom_lilac": blossom(rng, (188, 160, 232, 255), (255, 236, 150, 255)),
            "flower_sunflower": sunflower(rng), "flower_cosmos": cosmos(rng), "flower_tulip": tulip(rng), "leaf_sprig": sprig(rng)}
    for name, im in jobs.items():
        finish(im, out / (name + ".png"))
    print("wrote", ", ".join(jobs), "->", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "assets/collage/stickers")
