"""Rendering primitives for the story collages: anti-aliased torn paper, two-layer shadows, gaussian grain,
per-style colour grades, asset pack + font loading.

Design rules (RAM matters: the collage worker has 512 MB)
  * Only masks / edges are built at SS x (2x) and then downsampled with LANCZOS -> anti-aliased edges. The full
    1080x1920 canvas is NEVER supersampled; a card/mask exists at 2x only while that one card is being made.
  * Torn contour = fractal (2-3 octave) cosine-interpolated noise along the perimeter, not random points.
  * A torn photo card has two layers: an outer white paper rim (fibre fuzz) and the photo with its OWN ragged edge.
  * Shadows are two layers, warm brown, multiply blend: tight contact + soft ambient.
  * Grain is gaussian noise added to the whole canvas (zero mean), not sparse dots.
  * Everything that used to be drawn in code (tape, doodles, paper, torn scraps) is loaded from assets/collage/
    (PNG / JPG) when present. Every loader returns None / a procedural fallback when an asset is missing, so a
    checkout without the asset pack still renders.

Environment
  COLLAGE_ASSETS   override the asset dir (default <repo>/assets/collage)
"""
import logging
import math
import os
import random
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageStat

log = logging.getLogger(__name__)

SS = 2                                                           # supersampling factor for masks / edges only
ROOT = Path(__file__).resolve().parents[2]
ASSET_DIR = Path(os.getenv("COLLAGE_ASSETS") or (ROOT / "assets" / "collage"))
PROJECT_FONTS = ROOT / "fonts"

import asset_hub as hub               # shared asset pools: assets/collage FIRST, then assets/vibe
CANVAS = (1080, 1920)
LANCZOS, BICUBIC = Image.Resampling.LANCZOS, Image.Resampling.BICUBIC


# =============================================================== fonts
# key -> (file in assets/collage/fonts, fallback file in <repo>/fonts). All Google Fonts (OFL, free).
FAMILIES = {
    "caveat": ("Caveat.ttf", "Lora-Italic-Variable.ttf"),                 # handwriting
    "reenie": ("ReenieBeanie.ttf", "Lora-Italic-Variable.ttf"),           # handwriting (scribbly)
    "gaegu": ("Gaegu-Bold.ttf", "Poppins-Medium.ttf"),                    # handwriting (rounded)
    "elite": ("SpecialElite.ttf", "Poppins-Medium.ttf"),                  # typewriter
    "playfair": ("PlayfairDisplay.ttf", "Lora-Italic-Variable.ttf"),      # magazine serif
    "playfair_i": ("PlayfairDisplay-Italic.ttf", "Lora-Italic-Variable.ttf"),
    "bodoni": ("BodoniModa.ttf", "Lora-Italic-Variable.ttf"),
    "bodoni_i": ("BodoniModa-Italic.ttf", "Lora-Italic-Variable.ttf"),
    "anton": ("Anton.ttf", "Poppins-Bold.ttf"),                           # street
    "bebas": ("BebasNeue.ttf", "Poppins-Bold.ttf"),
    "marker": ("PermanentMarker.ttf", "Poppins-Bold.ttf"),
}
SCRAP_LETTER_FONTS = ("anton", "playfair", "elite", "marker", "caveat", "bebas", "bodoni", "gaegu")
_SYSTEM_FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _apply_variation(f, size, weight):
    """Variable Google fonts (Caveat, Playfair, Bodoni Moda): set weight, and the optical size to the pixel size."""
    try:
        axes = f.get_variation_axes()
    except Exception:
        return
    vals = []
    for a in axes:
        nm = a["name"]; nm = (nm.decode("latin-1") if isinstance(nm, bytes) else str(nm)).lower()
        if "weight" in nm and weight:
            v = weight
        elif "optical" in nm or nm == "opsz":
            v = size
        else:
            v = a["default"]
        vals.append(min(max(v, a["minimum"]), a["maximum"]))
    try:
        f.set_variation_by_axes(vals)
    except Exception as e:
        log.debug("font variation not applied: %s", e)


@lru_cache(maxsize=128)
def family_font(key, size, weight=None):
    """Font for a FAMILIES key, via the shared asset hub: this engine's pool
    (assets/collage/fonts) first, then the vibe pool, the project fonts and
    the system fallback."""
    main, fallback = FAMILIES[key]
    return hub.load_font((main, fallback), size, weight=weight,
                         first_dir=ASSET_DIR / "fonts")


def has_font(key):
    return hub.find("fonts", FAMILIES[key][0], first_dir=ASSET_DIR / "fonts") is not None


# =============================================================== asset loading (cached, small)
@lru_cache(maxsize=48)
def _asset(rel):
    return hub.load_rel(rel, first_dir=ASSET_DIR)


def asset(rel):
    """Copy of an asset as RGBA / L, or None when missing. Shared pools via the
    hub: assets/collage/<rel> first, then the same folder under assets/vibe."""
    im = _asset(rel)
    return im.copy() if im is not None else None


def asset_names(subdir):
    """Asset names of a kind across BOTH pools (manifest + directory scan)."""
    return hub.list_names(subdir)


def np_rng(rng):
    """numpy Generator derived from a python Random (keeps every render reproducible for the same photos / theme)."""
    return np.random.default_rng(rng.getrandbits(48))


def gauss_L(size, nrng, sigma, mean=128):
    w, h = size
    a = nrng.standard_normal((h, w), dtype=np.float32); a *= sigma; a += mean
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "L")


def tint_rgba(im, rgb):
    """Multiply the colour of an RGBA sprite by `rgb`; alpha untouched. White -> rgb, dark ink stays dark."""
    if not rgb:
        return im
    r, g, b, a = im.split()
    solid = Image.new("RGB", im.size, tuple(rgb[:3]))
    out = ImageChops.multiply(Image.merge("RGB", (r, g, b)), solid)
    out.putalpha(a)
    return out


# =============================================================== blending helpers
def blit(canvas, im, x, y):
    """alpha_composite tolerant of negative / overflowing positions."""
    x, y = int(x), int(y)
    l, t = max(0, -x), max(0, -y)
    r, b = min(im.width, canvas.width - x), min(im.height, canvas.height - y)
    if r > l and b > t:
        canvas.alpha_composite(im.crop((l, t, r, b)), (x + l, y + t))


def cast_shadow(canvas, im, x, y, color=(86, 56, 34), mode="multiply", contact=(3.5, (2, 4), 0.62),
                ambient=(22, (8, 17), 0.38), scale=1.0):
    """Two-layer shadow of `im`'s alpha under (x, y): tight contact (blur ~3.5) + soft ambient (blur ~22), warm brown,
    multiply blend. mode="normal" paints plain black instead (use on dark backgrounds where multiply is invisible).
    `scale` multiplies both strengths (tape / stickers use less). Draws only the shadow; call blit() for the image."""
    alpha = im.getchannel("A")
    for blur, (ox, oy), strength in (ambient, contact):                  # ambient first, contact on top
        strength = min(1.0, strength * scale)
        if strength <= 0:
            continue
        pad = int(blur * 3) + max(abs(ox), abs(oy)) + 2
        bw, bh = im.width + 2 * pad, im.height + 2 * pad
        sx, sy = int(x) - pad, int(y) - pad
        cl, ct = max(0, sx), max(0, sy)
        cr, cb = min(canvas.width, sx + bw), min(canvas.height, sy + bh)
        if cr <= cl or cb <= ct:
            continue
        m = Image.new("L", (bw, bh), 0)
        m.paste(alpha, (pad + ox, pad + oy))
        m = m.filter(ImageFilter.GaussianBlur(blur)).point(lambda a, s=strength: int(a * s))
        m = m.crop((cl - sx, ct - sy, cr - sx, cb - sy))
        region = canvas.crop((cl, ct, cr, cb)).convert("RGB")
        if mode == "multiply":
            dark = ImageChops.multiply(region, Image.new("RGB", region.size, color))
        else:
            dark = Image.new("RGB", region.size, (0, 0, 0))
        canvas.paste(Image.composite(dark, region, m).convert("RGBA"), (cl, ct))


def place(canvas, im, x, y, shadow=True, **kw):
    """Shadow + image in one call."""
    if shadow:
        cast_shadow(canvas, im, x, y, **kw)
    blit(canvas, im, x, y)


# =============================================================== noise + torn contours
def _periodic_fbm(n, rng, wavelength, octaves=3, gain=0.5, lacunarity=2.3):
    """Smooth periodic fractal noise in [-1, 1], n samples, cosine-interpolated value noise (2-3 octaves)."""
    out = np.zeros(n, np.float32); amp, wl, tot = 1.0, float(wavelength), 0.0
    xs = np.arange(n, dtype=np.float32)
    for _ in range(octaves):
        k = max(3, int(round(n / max(wl, 1.0))))
        ctrl = np.array([rng.uniform(-1, 1) for _ in range(k)], np.float32)
        x = xs * k / n; i0 = np.floor(x).astype(np.int64); t = x - i0
        i0 %= k; i1 = (i0 + 1) % k; t = (1 - np.cos(t * np.pi)) * 0.5
        out += amp * (ctrl[i0] * (1 - t) + ctrl[i1] * t)
        tot += amp; amp *= gain; wl /= lacunarity
    return out / tot


def torn_contour(w, h, rng, amp, inset=None, wavelength=70.0, octaves=3, step=3.0):
    """Closed polygon (list of (x, y)) running round a w x h rectangle, pushed in/out along the normal by smooth
    fractal noise of +-amp px. Every side gets its own strength so the edge is not uniformly ragged. All units px."""
    inset = amp if inset is None else inset
    x0, y0, x1, y1 = inset, inset, w - inset, h - inset
    a, b = x1 - x0, y1 - y0
    perim = 2 * (a + b); n = max(16, int(perim / step))
    d = np.clip(_periodic_fbm(n, rng, wavelength, octaves) * 1.9, -1, 1) * amp      # fbm is soft: stretch so +-amp is really used
    s = np.arange(n, dtype=np.float32) * perim / n
    sw = np.array([rng.uniform(0.65, 1.25) for _ in range(4)], np.float32)
    top, right, bottom = s < a, (s >= a) & (s < a + b), (s >= a + b) & (s < 2 * a + b)
    px = np.where(top, x0 + s, np.where(right, x1, np.where(bottom, x1 - (s - a - b), x0)))
    py = np.where(top, y0, np.where(right, y0 + (s - a), np.where(bottom, y1, y1 - (s - 2 * a - b))))
    nx = np.where(top, 0, np.where(right, 1, np.where(bottom, 0, -1)))
    ny = np.where(top, -1, np.where(right, 0, np.where(bottom, 1, 0)))
    k = np.where(top, sw[0], np.where(right, sw[1], np.where(bottom, sw[2], sw[3])))
    px = np.clip(px + nx * d * k, 0, w); py = np.clip(py + ny * d * k, 0, h)
    return list(zip(px.tolist(), py.tolist()))


def _ramp(lo, hi):
    return lambda v: 0 if v <= lo else 255 if v >= hi else int((v - lo) * 255 / (hi - lo))


def _fibre_fuzz(m2, nrng, band=3):
    """Paper-fibre fuzz on the edge of a 2x mask: the interior stays solid, the outside stays empty, and inside a thin
    band (+-band px) the edge is eaten / extended by clumpy noise, which reads as torn paper fibres once downsampled."""
    w, h = m2.size
    k = band * 2 + 1
    core, outer = m2.filter(ImageFilter.MinFilter(k)), m2.filter(ImageFilter.MaxFilter(k))
    blurred = m2.filter(ImageFilter.GaussianBlur(1.6))
    small = gauss_L((max(2, w // SS), max(2, h // SS)), nrng, 70)           # 1x noise stretched to 2x = clumpy fibres
    noise = ImageOps.autocontrast(small.resize((w, h), BICUBIC).filter(ImageFilter.GaussianBlur(0.6)))
    fuzzy = ImageChops.add(blurred, noise, 1.0, -128).point(_ramp(110, 150))
    return ImageChops.lighter(core, ImageChops.darker(fuzzy, outer))


def torn_mask(size, rng, amp=6, wavelength=70.0, fibre=True, inset=None, band=3):
    """Anti-aliased torn-paper mask (L, `size`): contour built at 2x from fractal noise, fibre fuzz, LANCZOS down."""
    w, h = size
    pts = torn_contour(w * SS, h * SS, rng, amp * SS, (amp if inset is None else inset) * SS, wavelength * SS, step=SS * 1.6)
    m2 = Image.new("L", (w * SS, h * SS), 0)
    ImageDraw.Draw(m2).polygon(pts, fill=255)
    m2 = _fibre_fuzz(m2, np_rng(rng), band) if fibre else m2.filter(ImageFilter.GaussianBlur(0.7))
    return m2.resize((w, h), LANCZOS)


def grow(mask, px, thresh=0.05):
    """Fast dilation of an L mask by ~px (blur + threshold; MaxFilter would be far too slow at 2x). Soft edge kept."""
    if px <= 0:
        return mask
    sigma = px / 1.645 if thresh <= 0.05 else px / 1.0
    lo = int(255 * thresh)
    return mask.filter(ImageFilter.GaussianBlur(sigma)).point(_ramp(lo, lo + 22))


# =============================================================== paper
def procedural_paper(size, seed=1, kind="fine"):
    """Greyscale paper texture (L, light, mean ~243): mottling + tooth + fibres; "crumple" adds creases, "ruled" lines.
    Used to build the shipped assets (tools/make_collage_assets.py) and as the runtime fallback."""
    w, h = size; r = np.random.default_rng(seed)

    def lowfreq(cell, amp):
        n = r.standard_normal((h // cell + 3, w // cell + 3)).astype(np.float32)
        im = Image.fromarray(np.clip(n * 40 + 128, 0, 255).astype(np.uint8), "L").resize((w, h), BICUBIC)
        return (np.asarray(im, np.float32) - 128) / 40 * amp

    a = np.full((h, w), 244, np.float32)
    a += lowfreq(max(8, w // 6), 2.4) + lowfreq(max(6, w // 20), 1.7) + lowfreq(max(4, w // 70), 1.1)
    a += r.standard_normal((h, w)).astype(np.float32) * 1.5
    fib = Image.new("L", (w, h), 128); fd = ImageDraw.Draw(fib)
    for _ in range(int(w * h / 420)):
        x, y = r.uniform(0, w), r.uniform(0, h); ang = r.uniform(0, math.pi); ln = r.uniform(3, 11)
        fd.line((x, y, x + ln * math.cos(ang), y + ln * math.sin(ang)), fill=int(r.choice((96, 176))), width=1)
    fib = fib.filter(ImageFilter.GaussianBlur(0.5))
    a += (np.asarray(fib, np.float32) - 128) * 0.10
    if kind == "crumple":
        lf = lowfreq(max(10, w // 8), 1.0) + lowfreq(max(6, w // 24), 0.7)
        gy, gx = np.gradient(lf); a += (gx + gy) * 9.0
        for _ in range(7):                                                   # a few soft creases
            x0, y0 = r.uniform(0, w), r.uniform(0, h); ang = r.uniform(0, math.pi); ln = r.uniform(w * .3, w * 1.1)
            cr = Image.new("L", (w, h), 0)
            ImageDraw.Draw(cr).line((x0, y0, x0 + ln * math.cos(ang), y0 + ln * math.sin(ang)), fill=255, width=2)
            a -= np.asarray(cr.filter(ImageFilter.GaussianBlur(2.2)), np.float32) / 255 * 7
    if kind == "ruled":
        ln_im = Image.new("L", (w, h), 0); ld = ImageDraw.Draw(ln_im)
        step = max(8, int(h * 54 / 1920)); y = int(h * 140 / 1920)
        while y < h:
            ld.line((0, y, w, y), fill=255, width=max(1, int(round(w / 540)))); y += step
        a -= np.asarray(ln_im.filter(ImageFilter.GaussianBlur(0.4)), np.float32) / 255 * 38
    return Image.fromarray(np.clip(a, 190, 255).astype(np.uint8), "L")


@lru_cache(maxsize=4)
def paper_texture(kind="fine"):
    """(L texture sized to the 1080x1920 canvas, its mean). Asset papers/<kind>.jpg, else procedural fallback."""
    im = None
    for ext in ("jpg", "png"):
        a = _asset("papers/%s.%s" % (kind, ext))
        if a is not None:
            im = a.convert("L"); break
    if im is None:
        im = procedural_paper((CANVAS[0] // 2, CANVAS[1] // 2), seed=sum(map(ord, kind)), kind=kind)
    im = im.resize(CANVAS, BICUBIC)
    return im, max(1.0, ImageStat.Stat(im).mean[0])


def paper_bg(color, kind="fine", strength=1.0):
    """Canvas-sized RGBA background: `color` x paper texture (multiply, brightness-compensated)."""
    tex, mean = paper_texture(kind)
    comp = 255.0 / mean
    base = Image.new("RGB", CANVAS, tuple(min(255, int(c * comp)) for c in color[:3]))
    out = ImageChops.multiply(base, tex.convert("RGB"))
    if strength < 1.0:
        out = Image.blend(Image.new("RGB", CANVAS, tuple(color[:3])), out, strength)
    return out.convert("RGBA")


def paper_patch(size, rng, tint=(252, 250, 245)):
    """RGB patch of paper (tinted, textured) of `size`, cropped from a random place of the paper texture."""
    tex, mean = paper_texture("fine")
    w, h = size
    if w > tex.width or h > tex.height:
        tex = tex.resize((max(w, tex.width), max(h, tex.height)), BICUBIC)
    x, y = rng.randint(0, tex.width - w), rng.randint(0, tex.height - h)
    comp = 255.0 / mean
    base = Image.new("RGB", size, tuple(min(255, int(c * comp)) for c in tint[:3]))
    return ImageChops.multiply(base, tex.crop((x, y, x + w, y + h)).convert("RGB"))


# =============================================================== torn photo card
def torn_card(tile, rng, border=14, amp=6, angle=0, torn=True, paper=(252, 250, 245), photo_amp=None):
    """Photo with a paper rim -> RGBA (rotated by `angle`).
    torn=True : outer layer = white paper rim with a fractal torn contour + fibre fuzz;
                inner layer = the photo with its own, different ragged contour (a torn photo shows the white paper under it).
    torn=False: clean paper border (anti-aliased by the rotation only).
    Masks are built at 2x and downsampled; the photo pixels stay at 1x."""
    tile = tile.convert("RGB"); w, h = tile.size
    cw, ch = w + 2 * border, h + 2 * border
    rim = paper_patch((cw, ch), rng, paper)
    ph = rim.copy(); ph.paste(tile, (border, border))
    if torn:
        amp = min(amp, max(2, border * 0.62))
        outer = torn_mask((cw, ch), rng, amp=amp, wavelength=46, fibre=True)
        pa = photo_amp if photo_amp is not None else max(2.0, amp * 0.6)
        # photo contour sits fully INSIDE the tile (inset = border + pa) so its ragged edge is always visible over the rim
        pts = torn_contour(cw * SS, ch * SS, rng, pa * SS, (border + pa * 0.9) * SS, 34 * SS, step=SS * 1.6)
        pm = Image.new("L", (cw * SS, ch * SS), 0); ImageDraw.Draw(pm).polygon(pts, fill=255)
        pm = _fibre_fuzz(pm, np_rng(rng), 2).resize((cw, ch), LANCZOS)
        card = rim.convert("RGBA"); card.paste(ph.convert("RGBA"), (0, 0), pm)
        card.putalpha(outer)
    else:
        card = ph.convert("RGBA")
    if angle:
        card = card.rotate(angle, expand=True, resample=BICUBIC)
    return card


def die_cut(tile, mask, outline=14, color=(255, 255, 255)):
    """Tile with a smooth white die-cut outline round the masked subject (outline is built at 2x -> anti-aliased)."""
    tile = tile.convert("RGB"); w, h = tile.size
    m = mask.filter(ImageFilter.GaussianBlur(0.8))
    ring = grow(m.resize((w * SS, h * SS), BICUBIC), outline * SS).resize((w, h), LANCZOS)
    out = tile.convert("RGBA")
    out.paste(Image.new("RGBA", (w, h), tuple(color) + (255,)), (0, 0), ring)
    out.paste(tile.convert("RGBA"), (0, 0), m)
    return out


# =============================================================== torn-paper die-cut (ragged white outline)
def _smooth_noise(shape, nrng, cell):
    """Smooth 2-D value noise (float32, h x w), zero mean / unit std. `cell` = feature size in px."""
    h, w = shape
    g = Image.fromarray(nrng.standard_normal((max(2, h // cell + 2), max(2, w // cell + 2))).astype(np.float32), "F")
    a = np.asarray(g.resize((w, h), BICUBIC), np.float32)
    return (a - a.mean()) / (a.std() + 1e-6)


def _dist_outside(inside):
    """float32 distance (px) from every pixel to the nearest True pixel of `inside` (0 inside). None if no backend."""
    try:
        import cv2
        return cv2.distanceTransform((~inside).astype(np.uint8), cv2.DIST_L2, 5)
    except Exception:
        pass
    try:
        from scipy import ndimage
        return ndimage.distance_transform_edt(~inside).astype(np.float32)
    except Exception:
        return None


def torn_die_cut(tile, mask, outline=16, rng=None, wobble=0.55, paper=(252, 250, 245)):
    """Cut-out subject with a RAGGED torn-paper white outline (not a clean offset ring).

    The mask is dilated by a distance field whose reach is wobbled by noise: big slow bumps (tears), medium bumps
    and a fine tooth, then the very edge gets a little fibre fuzz. The ring is paper-textured, not flat white.
    Works at 1x (distance is float, so the edge is anti-aliased by its own 1 px ramp) -> light on a 512 MB worker.
    Returns (sprite RGBA, (ox, oy)): sprite's top-left relative to the tile's origin (may be negative), or None
    when the mask is empty."""
    rng = rng or random.Random(0)
    tile = tile.convert("RGB")
    m = mask.convert("L").filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.9))   # 1 px erode: no bg halo
    bbox = m.point(lambda a: 255 if a > 40 else 0).getbbox()
    if not bbox:
        return None
    pad = int(outline * (1 + wobble) + 8)
    x0, y0, x1, y1 = bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad
    w, h = x1 - x0, y1 - y0
    sm = Image.new("L", (w, h), 0); sm.paste(m.crop(bbox), (pad, pad))
    sa = np.asarray(sm, np.float32) / 255.0
    nrng = np_rng(rng)
    dist = _dist_outside(sa > 0.5)
    if dist is not None:
        low = np.clip(_smooth_noise((h, w), nrng, 34), -2, 2)
        mid = np.clip(_smooth_noise((h, w), nrng, 12), -2, 2)
        fine = _smooth_noise((h, w), nrng, 4)
        reach = outline * (1 + wobble * (0.7 * low + 0.3 * mid)) + 1.5 * fine
        reach = np.maximum(reach, outline * 0.4)
        ring = np.clip(reach - dist + 0.5, 0, 1)
        edge = np.clip(1 - (reach - dist) / 3.0, 0, 1) * (dist > 0)          # outer ~3 px: paper fibres
        fuzz = _smooth_noise((h, w), nrng, 2)
        ring *= 1 - edge * 0.38 * (0.5 + 0.5 * np.clip(fuzz, -1, 1))
    else:                                                                       # no cv2 / scipy: noise-modulated threshold
        field = np.asarray(sm.filter(ImageFilter.GaussianBlur(outline * 0.9)), np.float32) / 255.0
        thr = 0.14 + 0.07 * np.clip(_smooth_noise((h, w), nrng, 14), -2, 2)
        ring = np.clip((field - thr) * 14 + 0.5, 0, 1)
    ring_img = Image.fromarray((np.clip(np.maximum(ring, sa), 0, 1) * 255).astype(np.uint8), "L")
    out = paper_patch((w, h), rng, paper).convert("RGBA")
    out.putalpha(ring_img)
    sub = Image.new("RGB", (w, h), (0, 0, 0)); sub.paste(tile.crop(bbox), (pad, pad))
    sub = sub.convert("RGBA"); sub.putalpha(sm)
    out.alpha_composite(sub)
    return out, (x0, y0)


# =============================================================== sprites from the asset pack
def sticker(name, size, fill=None, angle=0, rng=None):
    """Hand-drawn doodle sprite from assets/collage/stickers/<name>.png scaled so its longer side is `size`.
    White parts take colour `fill`, the dark outline stays dark. None when the asset does not exist."""
    im = asset("stickers/%s.png" % name)
    if im is None:
        return None
    im = im.convert("RGBA"); s = size / max(im.size)
    im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), LANCZOS)
    if fill:
        im = tint_rgba(im, fill)
    if angle:
        im = im.rotate(angle, expand=True, resample=BICUBIC)
    return im


def tape_sprite(kind, w, h, angle, rng, tint=None):
    """Tape strip RGBA from assets/collage/tapes/<kind>.png (stretched to w x h, random flip, tinted, rotated).
    Falls back to a procedural strip with zig-zag torn ends when the asset is missing."""
    im = asset("tapes/%s.png" % kind)
    if im is not None:
        im = im.convert("RGBA").resize((w, h), LANCZOS)
        if rng.random() < 0.5:
            im = ImageOps.mirror(im)
        im = tint_rgba(im, tint)
    else:
        col = tint or (236, 226, 186)
        im = Image.new("RGBA", (w, h), tuple(col) + (205,)); d = ImageDraw.Draw(im)
        for k in range(0, w, 7):
            d.line((k, 0, k + rng.randint(-2, 2), h), fill=tuple(col) + (232,), width=1)
        im.putalpha(ImageChops.multiply(im.getchannel("A"), torn_mask((w, h), rng, amp=2, wavelength=14, fibre=False)))
    return im.rotate(angle, expand=True, resample=BICUBIC)


def scrap(size, rng, tint=(252, 250, 244)):
    """Torn paper scrap RGBA of `size` (strip / label / patch): asset scraps/*.png stretched, else a procedural one."""
    w, h = size
    names = [n for n in asset_names("scraps")]
    pick = None
    if names:
        ratio = w / h
        cands = [n for n in names if _asset("scraps/%s.png" % n) is not None]
        cands.sort(key=lambda n: abs(math.log(_asset("scraps/%s.png" % n).width / _asset("scraps/%s.png" % n).height / ratio)))
        pick = rng.choice(cands[:2]) if cands else None
    if pick:
        im = asset("scraps/%s.png" % pick).convert("RGBA").resize((w, h), LANCZOS)
        if rng.random() < 0.5:
            im = ImageOps.mirror(im)
        return tint_rgba(im, tint)
    pm = torn_mask((w, h), rng, amp=6, wavelength=60)
    out = paper_patch((w, h), rng, tint).convert("RGBA"); out.putalpha(pm)
    return out


def torn_tile(size, rng, fill, amp=3):
    """Small torn paper tile filled with a flat colour (ransom-note letters)."""
    t = Image.new("RGBA", size, tuple(fill[:3]) + (255,))
    t.putalpha(torn_mask(size, rng, amp=amp, wavelength=22, band=2))
    return t


# =============================================================== grain
def grain(canvas, rng, opacity=0.06):
    """Gaussian noise over the whole canvas. `opacity` ~ 0.04-0.08: pixel noise has std 255*opacity/2 (zero mean, so the
    paper does not get darker). Dots are gone; this is film / paper grain."""
    rgb = canvas.convert("RGB")
    n = gauss_L(rgb.size, np_rng(rng), 255 * opacity * 0.5)
    out = ImageChops.add(rgb, Image.merge("RGB", (n, n, n)), 1.0, -128)
    return out.convert("RGBA")


# =============================================================== colour grades
# sat/contrast/bright: PIL enhance factors; lift: raised black point (0-40, matte "fade"); tint: per-channel gain;
# scurve: 0-1 contrast curve strength; mono: greyscale first (autocontrast cutoff 1)
GRADES = {
    "playful_hype":      dict(sat=1.16, contrast=1.07, bright=1.02, lift=0, tint=(1.0, 1.0, 1.0)),
    "kpop_editorial":    dict(mono=True, contrast=1.15, lift=10, tint=(0.97, 0.96, 1.04), scurve=0.25),
    "photo_dump":        dict(sat=0.82, contrast=1.06, lift=16, tint=(1.04, 1.0, 0.90)),            # warm fade
    "streetwear_baddie": dict(mono=True, contrast=1.35, scurve=0.55, lift=0),                        # high contrast B&W
    "classy_editorial":  dict(sat=0.60, contrast=0.97, lift=9, tint=(1.02, 0.99, 0.94)),
    "scrapbook_chaos":   dict(sat=1.06, contrast=1.04, lift=6, tint=(1.03, 1.0, 0.95)),
}


def _lut(lift, gain, scurve):
    out = []
    for v in range(256):
        s = v / 255.0
        if scurve:
            s = s * (1 - scurve) + (s * s * (3 - 2 * s)) * scurve
        s = lift / 255.0 + s * (1 - lift / 255.0)
        out.append(max(0, min(255, int(round(s * 255 * gain)))))
    return out


def grade(im, name):
    """Per-style colour grade of a photo tile (RGB in, RGB out, same size)."""
    g = GRADES.get(name)
    im = im.convert("RGB")
    if not g:
        return im
    if g.get("mono"):
        im = ImageOps.autocontrast(ImageOps.grayscale(im), cutoff=1).convert("RGB")
    if g.get("sat", 1) != 1:
        im = ImageEnhance.Color(im).enhance(g["sat"])
    if g.get("contrast", 1) != 1:
        im = ImageEnhance.Contrast(im).enhance(g["contrast"])
    if g.get("bright", 1) != 1:
        im = ImageEnhance.Brightness(im).enhance(g["bright"])
    tint = g.get("tint", (1.0, 1.0, 1.0))
    lut = []
    for c in range(3):
        lut += _lut(g.get("lift", 0), tint[c], g.get("scurve", 0))
    return im.point(lut)
