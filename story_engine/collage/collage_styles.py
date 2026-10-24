"""Six story collage styles (1080x1920), modelled on the Aesthetic Styles Guide:
playful_hype, kpop_editorial, photo_dump, streetwear_baddie, classy_editorial, scrapbook_chaos.

Everything is drawn with Pillow. Subject cut-outs (rembg) are optional: every style has a no-cutout fallback.
Fonts available in the project: Poppins (Bold/Medium/Light) and Lora Italic - the "handwriting" and "typewriter"
looks of the reference are approximated with those.
"""
from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageFilter, ImageEnhance, ImageChops
from pathlib import Path
from datetime import datetime, timezone
import io, math, os, random, logging

log = logging.getLogger(__name__)
W, H = 1080, 1920
_FONTS = Path(__file__).resolve().parents[2] / "fonts"
_FILES = {"bold": "Poppins-Bold.ttf", "medium": "Poppins-Medium.ttf", "light": "Poppins-Light.ttf",
          "serif": "Lora-Italic-Variable.ttf"}
THEMES = ("playful_hype", "kpop_editorial", "photo_dump", "streetwear_baddie", "classy_editorial", "scrapbook_chaos")


# ------------------------------------------------------------------ helpers
def font(kind, size):
    for p in (_FONTS / _FILES[kind], Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")):
        try:
            return ImageFont.truetype(str(p), size)
        except Exception:
            continue
    return ImageFont.load_default()


def _load(p):
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(p["bytes"]))).convert("RGB"); im.thumbnail((1600, 1600)); return im


def fit(p, size):
    from .collage_renderer import _fit_focus
    return _fit_focus(_load(p), size, p.get("focus"))


def blit(canvas, im, x, y):
    """alpha_composite that tolerates negative / overflowing positions."""
    x, y = int(x), int(y)
    l, t = max(0, -x), max(0, -y)
    r, b = min(im.width, canvas.width - x), min(im.height, canvas.height - y)
    if r > l and b > t:
        canvas.alpha_composite(im.crop((l, t, r, b)), (x + l, y + t))


def torn_edge(size, rng, amp=6, step=14):
    w, h = size; pts = []
    for x in range(0, w + step, step): pts.append((min(w, x), rng.randint(0, amp)))
    for y in range(0, h + step, step): pts.append((rng.randint(w - amp, w), min(h, y)))
    for x in range(w, -step, -step): pts.append((max(0, x), rng.randint(h - amp, h)))
    for y in range(h, -step, -step): pts.append((rng.randint(0, amp), max(0, y)))
    m = Image.new("L", (w, h), 0); ImageDraw.Draw(m).polygon(pts, fill=255); return m


def card(im, rng, border=12, torn=True, amp=6, angle=0, color=(255, 255, 255)):
    """Photo with a paper border (optionally torn), rotated; returns RGBA."""
    w, h = im.size
    base = Image.new("RGBA", (w + border * 2, h + border * 2), color + (255,)); base.alpha_composite(im.convert("RGBA"), (border, border))
    if torn:
        base.putalpha(torn_edge(base.size, rng, amp=amp, step=max(10, w // 40)))
    if angle:
        base = base.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
    return base


def shadowed(canvas, im, x, y, blur=12, off=(6, 10), strength=0.5):
    sh = Image.new("RGBA", im.size, (0, 0, 0, 0))
    sh.putalpha(im.getchannel("A").filter(ImageFilter.GaussianBlur(blur)).point(lambda a: int(a * strength)))
    blit(canvas, sh, x + off[0], y + off[1]); blit(canvas, im, x, y)


def tape(canvas, cx, cy, w, h, angle, rng, color=(236, 226, 186)):
    t = Image.new("RGBA", (w, h), color + (200,)); d = ImageDraw.Draw(t)
    for k in range(0, w, 7): d.line((k, 0, k + rng.randint(-2, 2), h), fill=color + (230,), width=1)
    t.putalpha(ImageChops.multiply(t.getchannel("A"), torn_edge((w, h), rng, amp=3, step=6)))
    t = t.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
    blit(canvas, t, cx - t.width // 2, cy - t.height // 2)


def grain(canvas, rng, n=3000, rgb=(60, 45, 30)):
    g = Image.new("RGBA", canvas.size, (0, 0, 0, 0)); d = ImageDraw.Draw(g)
    for _ in range(n): d.point((rng.randrange(canvas.width), rng.randrange(canvas.height)), fill=rgb + (rng.randrange(6, 22),))
    return Image.alpha_composite(canvas, g)


def cut_mask(tile):
    """Subject mask (L, same size as tile) via rembg, or None. Never raises."""
    if os.getenv("COLLAGE_CUTOUTS", "1").lower() in ("0", "false", "off", "no"):
        return None
    try:
        from . import collage_renderer as cr
        from rembg import remove, new_session
        if cr._SESSION is None:
            cr._SESSION = new_session(os.getenv("COLLAGE_REMBG_MODEL", "u2netp"))
        small = tile.copy(); small.thumbnail((640, 640)); raw = io.BytesIO(); small.save(raw, "PNG")
        a = Image.open(io.BytesIO(remove(raw.getvalue(), session=cr._SESSION))).convert("RGBA").getchannel("A")
        if sum(a.resize((16, 16)).tobytes()) < 0.06 * 255 * 256:     # < 6% subject => failed cut
            return None
        return a.resize(tile.size, Image.Resampling.LANCZOS)
    except Exception as e:
        log.info("cutout skipped: %s", e); return None


def die_cut(tile, mask, outline=14, color=(255, 255, 255)):
    """Tile with a white die-cut outline around the masked subject (outline sits ON the tile)."""
    out = tile.convert("RGBA")
    ring = mask.filter(ImageFilter.MaxFilter(outline * 2 + 1)).filter(ImageFilter.GaussianBlur(1.5)).point(lambda a: 255 if a > 100 else 0)
    out.paste(Image.new("RGBA", tile.size, color + (255,)), (0, 0), ring)
    out.paste(tile.convert("RGBA"), (0, 0), mask)
    return out


def wrap(draw, text, fnt, max_w, max_lines=3):
    lines, cur = [], ""
    for w_ in str(text).split():
        t = (cur + " " + w_).strip()
        if draw.textlength(t, font=fnt) <= max_w or not cur: cur = t
        else: lines.append(cur); cur = w_
    if cur: lines.append(cur)
    return lines[:max_lines]


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


def stroke_text(d, xy, text, fnt, fill, stroke=(255, 255, 255), sw=0, anchor=None):
    d.text(xy, text, font=fnt, fill=fill, stroke_width=sw, stroke_fill=stroke, anchor=anchor)


def spaced(d, xy, text, fnt, fill, gap=4):
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=fnt, fill=fill); x += d.textlength(ch, font=fnt) + gap


def _dt(p):
    v = p.get("scheduled_at")
    return v if isinstance(v, datetime) else datetime.now(timezone.utc)


def _png(canvas):
    out = io.BytesIO(); canvas.convert("RGB").save(out, "PNG", optimize=True); return out.getvalue()


def _rows(n):
    """Photos per row for a 2-column grid: odd count => last row is a single wide tile."""
    rows = [2] * (n // 2) + ([1] if n % 2 else [])
    return rows


# ------------------------------------------------------------------ 1. playful / hype
def playful_hype(photos, copy, rng):
    canvas = Image.new("RGBA", (W, H), (30, 30, 30, 255)); rows = _rows(len(photos)); rh = H // len(rows)
    yellow, green, orange = (255, 214, 60, 255), (120, 235, 160, 255), (255, 140, 110, 255)
    i = 0
    for r, cols in enumerate(rows):
        for c in range(cols):
            tw = W // cols; x, y = c * tw, r * rh; p = photos[i]; i += 1
            tile = fit(p, (tw, rh)); mask = cut_mask(tile)
            if mask is not None:
                canvas.alpha_composite(die_cut(tile, mask), (x, y))
            else:                                               # fallback: torn white frame inside the tile
                t = tile.convert("RGBA"); frame = Image.new("RGBA", t.size, (255, 255, 255, 255))
                inner = torn_edge((tw - 60, rh - 60), rng, amp=9, step=16); hole = Image.new("L", t.size, 255); hole.paste(0, (30, 30), inner)
                frame.putalpha(hole); t.alpha_composite(frame); canvas.alpha_composite(t, (x, y))
    d = ImageDraw.Draw(canvas)
    for k in range(len(photos) * 2 + 2):                         # doodles on tile corners, on top of everything
        cx, cy = rng.choice([rng.randint(60, W - 60), rng.choice([70, W - 70])]), rng.randint(80, H - 80)
        kind = k % 4; col = (yellow, green, orange, yellow)[kind]; r_ = rng.randint(26, 54)
        if kind == 0: sparkle(d, cx, cy, r_, yellow)
        elif kind == 1: heart(d, cx, cy, r_, green)
        elif kind == 2: exclaim(d, cx, cy, r_ * 1.2, orange)
        else: sun(d, cx, cy, int(r_ * 1.4), yellow)
    text = str(copy.get("text", "")).upper()
    if text:
        f = font("bold", 74); lines = wrap(d, text, f, W - 120, 2); y = H - 150 - 90 * len(lines)
        for ln in lines:
            stroke_text(d, (W // 2, y), ln, f, (255, 255, 255), (20, 20, 20), 6, anchor="ma"); y += 90
    return canvas


# ------------------------------------------------------------------ 2. k-pop editorial
def kpop_editorial(photos, copy, rng):
    canvas = Image.new("RGBA", (W, H), (242, 242, 246, 255)); n = len(photos)
    purple, orchid = (123, 108, 255, 235), (205, 110, 255, 235)
    rows = _rows(n); th = (H - 120) // len(rows); y = 60; i = 0
    for r, cols in enumerate(rows):
        tw = W if cols == 1 else int(W * .53); order = []
        for c in range(cols):
            p = photos[i]; i += 1
            tile = ImageOps.autocontrast(ImageOps.grayscale(fit(p, (tw, th))), cutoff=1)
            tile = ImageEnhance.Contrast(tile).enhance(1.15).convert("RGB")
            x = 0 if c == 0 else W - tw
            col = purple if (r + c) % 2 == 0 else orchid
            t = tile.convert("RGBA"); d = ImageDraw.Draw(t)
            word = "VIBE" if cols == 2 else "VIBEOS"; fs = int(tw * (.62 if cols == 2 else .30))
            f = font("serif", fs)
            while d.textlength(word, font=f) > tw * .86 and fs > 60: fs -= 8; f = font("serif", fs)
            mask = cut_mask(tile.convert("RGB"))
            layer = Image.new("RGBA", t.size, (0, 0, 0, 0)); ImageDraw.Draw(layer).text((tw // 2 - (0 if cols == 1 else int(W * .015) * (1 if c == 0 else -1)), 10), word, font=f, fill=col, anchor="ma")
            t.alpha_composite(layer)
            if mask is not None:                                 # subject re-pasted over the masthead = overlap look
                t.paste(tile.convert("RGBA"), (0, 0), mask)
            lines = wrap(d, str(copy.get("text", "")).lower(), font("serif", int(th * .11)), tw - 60, 2)
            ImageDraw.Draw(t).text((30, th - 30 - len(lines) * int(th * .125)), "\n".join(lines), font=font("serif", int(th * .11)), fill=col)
            order.append((t, x))
        for t, x in sorted(order, key=lambda o: o[1], reverse=(r % 2 == 1)):   # alternate which tile overlaps
            blit(canvas, t, x, y)
        y += th
    return canvas


# ------------------------------------------------------------------ 3. photo dump
_CAPTIONS = ["morning brew", "on the way", "sunset views", "working work", "friend with friend", "little things", "good light", "random tuesday"]


def photo_dump(photos, copy, rng):
    canvas = Image.new("RGBA", (W, H), (234, 222, 195, 255)); d = ImageDraw.Draw(canvas)
    for yy in range(140, H, 54): d.line((0, yy, W, yy), fill=(205, 190, 158, 255), width=2)
    canvas = grain(canvas, rng, 6000)
    vig = Image.new("L", (W, H), 0); ImageDraw.Draw(vig).rectangle((40, 40, W - 40, H - 40), fill=255); vig = vig.filter(ImageFilter.GaussianBlur(60))
    canvas = Image.composite(canvas, Image.new("RGBA", (W, H), (150, 120, 80, 255)), vig)
    d = ImageDraw.Draw(canvas); n = len(photos)
    plan = {3: [(2, 0), (1, 1)], 4: [(2, 0), (2, 1)], 5: [(2, 0), (1, 1), (2, 2)]}[n] if n in (3, 4, 5) else [(2, 0)]
    rows = len(plan); slot_h = (H - 260) // rows; i = 0
    stamp = _dt(photos[0]).strftime("%b %d, '%y").upper()
    for cols, r in plan:
        pw = 440 if cols == 2 else 700; ph = min(slot_h - 150, 520 if cols == 2 else 430)
        for c in range(cols):
            p = photos[i]; im = fit(p, (pw, ph)); i += 1
            im = ImageEnhance.Color(im).enhance(.85); im = ImageEnhance.Contrast(im).enhance(1.08)
            warm = Image.new("RGB", im.size, (255, 214, 160)); im = Image.blend(im, warm, .10)
            cd = card(im, rng, border=8, torn=False, angle=rng.uniform(-3.2, 3.2), color=(24, 22, 20))
            cx = (W // (cols + 1)) * (c + 1); cy = 190 + r * slot_h + slot_h // 2 - 40
            shadowed(canvas, cd, cx - cd.width // 2, cy - cd.height // 2, blur=8, off=(4, 7), strength=.55)
            tape(canvas, cx + rng.randint(-40, 40), cy - cd.height // 2 + 6, 190, 52, rng.uniform(-6, 6), rng)
            d = ImageDraw.Draw(canvas)
            cap = str(p.get("subject") or "").strip().lower()
            cap = " ".join(cap.split()[:3]) if cap and cap not in ("none", "unknown") else _CAPTIONS[(i + len(photos)) % len(_CAPTIONS)]
            ty = cy + cd.height // 2 + 14
            if (i + r) % 2: d.text((cx, ty), cap, font=font("serif", 40), fill=(40, 34, 28), anchor="ma")
            else: d.text((cx, ty), stamp, font=font("medium", 34), fill=(50, 42, 34), anchor="ma")
    t = str(copy.get("text", "")).lower()
    if t: d.text((W // 2, H - 120), t, font=font("serif", 54), fill=(40, 34, 28), anchor="ma")
    return canvas


# ------------------------------------------------------------------ 4. streetwear / baddie
def streetwear_baddie(photos, copy, rng):
    canvas = Image.new("RGBA", (W, H), (230, 230, 224, 255)); canvas = grain(canvas, rng, 5000, (0, 0, 0))
    d = ImageDraw.Draw(canvas); neon = (182, 255, 60, 255); ink = (14, 14, 14, 255)
    issue = _dt(photos[0]).timetuple().tm_yday
    d.text((W // 2, 40), f"VIBEOS RAW / ISSUE #{issue:02d}", font=font("bold", 58), fill=ink, anchor="ma")
    px, py, pw, ph = 90, 150, 900, 1340                              # dark halftone panel
    hero = ImageOps.grayscale(fit(photos[0], (pw, ph))); hero = ImageEnhance.Contrast(ImageOps.autocontrast(hero, 2)).enhance(1.5)
    bg = hero.filter(ImageFilter.GaussianBlur(2)).point(lambda v: int(v * .55))
    dots = Image.new("L", (pw, ph), 255); dd = ImageDraw.Draw(dots); cell = 10
    small = bg.resize((pw // cell, ph // cell), Image.Resampling.BOX); sp = small.tobytes()
    for gy in range(small.height):
        for gx in range(small.width):
            rr = (1 - sp[gy * small.width + gx] / 255) * cell * .72
            dd.ellipse((gx * cell + cell / 2 - rr, gy * cell + cell / 2 - rr, gx * cell + cell / 2 + rr, gy * cell + cell / 2 + rr), fill=0)
    panel = Image.merge("RGBA", (dots, dots, dots, Image.new("L", (pw, ph), 255)))
    panel.putalpha(torn_edge((pw, ph), rng, amp=10, step=18)); canvas.alpha_composite(panel, (px, py))
    mask = cut_mask(hero.convert("RGB")); hh = int(ph * .78)
    sub = ImageOps.fit(hero.convert("RGB"), (int(pw * .62), hh), centering=(.5, .3))
    sx, sy = px + 30, py + ph - hh - 150
    if mask is not None:
        m2 = ImageOps.fit(mask, sub.size, centering=(.5, .3)); canvas.alpha_composite(die_cut(sub, m2, 12), (sx, sy))
    else:
        blit(canvas, card(sub, rng, border=12, amp=7, angle=-2), sx, sy)
    others = photos[1:]; oy = py + 60
    for k, p in enumerate(others):                                  # torn B&W snippets stacked on the right
        w_, h_ = 330, 230 if k % 2 == 0 else 270
        im = ImageEnhance.Contrast(ImageOps.grayscale(fit(p, (w_, h_)))).enhance(1.4).convert("RGB")
        cd = card(im, rng, border=10, amp=7, angle=rng.uniform(-4, 4))
        shadowed(canvas, cd, px + pw - w_ - 10 - rng.randint(0, 30), oy, blur=8, off=(5, 8)); tape(canvas, px + pw - w_ // 2, oy + 6, 150, 44, rng.uniform(-10, 10), rng, (190, 255, 70))
        oy += h_ + 56
    d = ImageDraw.Draw(canvas)
    for text, xy, sz in (("STYLE 01", (px + 10, py + 6), 60), ("FIT CHECK", (px + 330, py + 70), 50)):
        wdt = d.textlength(text, font=font("bold", sz)); fill = (255, 255, 255, 255) if text == "STYLE 01" else neon
        d.rectangle((xy[0] - 12, xy[1] - 6, xy[0] + wdt + 12, xy[1] + sz + 14), fill=fill); d.text(xy, text, font=font("bold", sz), fill=ink)
    d.rectangle((px + 20, sy + hh - 20, px + 300, sy + hh + 60), fill=neon); d.text((px + 40, sy + hh - 8), "MUST HAVE!", font=font("bold", 42), fill=ink)
    d.text((px - 6, 1452), "RAW", font=font("bold", 230), fill=ink)
    t = str(copy.get("text", "")).upper()
    d.text((px + 6, 1736), t[:36], font=font("medium", 34), fill=ink)
    d.text((px + 4, 1774), "ZINE CULTURE", font=font("bold", 72), fill=ink)
    side = Image.new("RGBA", (1250, 70), (0, 0, 0, 0)); ImageDraw.Draw(side).text((0, 0), "THE FIT IS READY  /  GRITTY LOOK  /  " + _dt(photos[0]).strftime("FALL '%y"), font=font("bold", 44), fill=ink)
    canvas.alpha_composite(side.rotate(-90, expand=True), (W - 82, 150)); return canvas


# ------------------------------------------------------------------ 5. classy editorial
def classy_editorial(photos, copy, rng):
    canvas = Image.new("RGBA", (W, H), (238, 236, 230, 255)); d = ImageDraw.Draw(canvas); ink = (30, 28, 26, 255)
    d.line((70, 90, W - 70, 90), fill=ink, width=3)
    d.text((70, 112), "N° " + _dt(photos[0]).strftime("%d"), font=font("medium", 26), fill=ink)
    d.text((W - 70, 112), _dt(photos[0]).strftime("%B %Y").upper(), font=font("medium", 26), fill=ink, anchor="ra")

    def muted(p, size):
        im = ImageEnhance.Color(fit(p, size)).enhance(.62); im = ImageEnhance.Contrast(im).enhance(.96)
        return Image.blend(im, Image.new("RGB", im.size, (214, 196, 170)), .10)
    plan = [(110, 190, 560, 860), (700, 330, 280, 360), (150, 1110, 330, 400), (560, 880, 420, 520), (720, 1450, 260, 320)]
    for p, (x, y, w, h) in zip(photos, plan):
        cd = card(muted(p, (w, h)), rng, border=14, torn=False, color=(250, 249, 245)); shadowed(canvas, cd, x, y, blur=14, off=(0, 8), strength=.22)
    d = ImageDraw.Draw(canvas)
    word = (str(copy.get("text", "")).split() or ["folk"])[0].strip(".,!?").capitalize()
    side = Image.new("RGBA", (900, 260), (0, 0, 0, 0)); ImageDraw.Draw(side).text((0, 0), word, font=font("serif", 200), fill=ink)
    bb = side.getbbox(); side = side.crop(bb).rotate(-90, expand=True); canvas.alpha_composite(side, (W - side.width - 60, 160))
    t = str(copy.get("text", "")).capitalize(); f = font("serif", 70); y = 1450
    for ln in wrap(d, t, f, 480, 3): d.text((110, y), ln, font=f, fill=ink); y += 84
    for k in range(5): d.line((110, 1700 + k * 22, 110 + rng.randint(260, 420), 1700 + k * 22), fill=(150, 146, 138, 255), width=3)
    d.line((70, H - 70, W - 70, H - 70), fill=ink, width=2); d.text((70, H - 62), "FOLK.", font=font("medium", 24), fill=ink)
    return canvas


# ------------------------------------------------------------------ 6. scrapbook chaos
def scrapbook_chaos(photos, copy, rng):
    canvas = Image.new("RGBA", (W, H), (243, 238, 228, 255)); canvas = grain(canvas, rng, 3500)
    plan = [(-30, -20, 640, 780, -3), (440, 40, 660, 560, 3), (-20, 620, 560, 700, 2), (420, 700, 700, 620, -3), (180, 1280, 760, 640, 1)]
    order = list(range(len(photos)))
    for idx in order:
        x, y, w, h, ang = plan[idx]; p = photos[idx]
        cd = card(fit(p, (w, h)), rng, border=14, amp=9, angle=ang); shadowed(canvas, cd, x, y, blur=12, off=(5, 9))
    d = ImageDraw.Draw(canvas)
    pal = [(231, 140, 120), (118, 90, 150), (240, 196, 70), (86, 130, 110), (176, 80, 90), (120, 150, 200), (240, 170, 190)]
    month = _dt(photos[0]).strftime("%B").lower(); words = [month, "dump"]
    y = 800
    for wd in words:
        x = (W - len(wd) * 124) // 2
        for ch in wd:
            col = pal[rng.randrange(len(pal))]; tile = Image.new("RGBA", (118, 138), (252, 248, 238, 255))
            td = ImageDraw.Draw(tile); td.rectangle((6, 6, 112, 132), fill=col + (255,)); td.text((59, 72), ch, font=font("bold", 98), fill=(252, 248, 238), anchor="mm")
            tile.putalpha(torn_edge(tile.size, rng, amp=4, step=9)); tile = tile.rotate(rng.uniform(-9, 9), expand=True, resample=Image.Resampling.BICUBIC)
            shadowed(canvas, tile, x, y + rng.randint(-8, 8), blur=5, off=(3, 5)); x += 124
        y += 150
    d = ImageDraw.Draw(canvas)
    for _ in range(5):
        flower(d, rng.randint(80, W - 80), rng.randint(120, H - 200), rng.randint(34, 58), (rng.choice([(247, 226, 140), (245, 190, 200), (255, 255, 255)])), (230, 150, 60))
    for _ in range(4): sparkle(d, rng.randint(60, W - 60), rng.randint(100, H - 100), rng.randint(18, 30), (255, 214, 80, 255), (40, 40, 40, 160))
    tape(canvas, 300, 70, 200, 56, -8, rng); tape(canvas, 820, 640, 190, 52, 12, rng); tape(canvas, 240, 1300, 190, 52, -14, rng)
    t = str(copy.get("text", "")).lower()
    strip = Image.new("RGBA", (W - 160, 120), (252, 250, 244, 255)); strip.putalpha(torn_edge(strip.size, rng, amp=7, step=12))
    ImageDraw.Draw(strip).text((strip.width // 2, 60), t, font=font("serif", 54), fill=(40, 34, 28), anchor="mm")
    shadowed(canvas, strip.rotate(-1.5, expand=True, resample=Image.Resampling.BICUBIC), 80, H - 190, blur=6, off=(3, 5))
    return canvas


STYLES = {"playful_hype": playful_hype, "kpop_editorial": kpop_editorial, "photo_dump": photo_dump,
          "streetwear_baddie": streetwear_baddie, "classy_editorial": classy_editorial, "scrapbook_chaos": scrapbook_chaos}


def render_style(photos, theme, copy):
    rng = random.Random(str(theme) + str([p.get("asset_id") for p in photos]))
    return _png(STYLES[theme](photos, copy, rng))
