"""Layered 1080x1920 story collage: paper grain, face-aware photo cards, one die-cut subject sticker, doodles.

Fixes vs the first version:
  * crops are centred on the detected face / subject (`focus`), not blindly on the image centre
  * only the hero photo gets a cutout sticker (before, every photo was pasted twice and looked doubled)
  * cutouts really work: rembg is optional, small model, input capped, any failure just skips the sticker
  * title wraps to the canvas instead of running off the right edge; project Poppins font is used
"""
from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageFilter
from pathlib import Path
import io, os, random, math, logging

log = logging.getLogger(__name__)
W, H = 1080, 1920
_FONTS = Path(__file__).resolve().parents[2] / "fonts"
_SESSION = None


def _font(size, bold=False):
    for p in (_FONTS / ("Poppins-Bold.ttf" if bold else "Poppins-Medium.ttf"),
              Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")):
        try:
            return ImageFont.truetype(str(p), size)
        except Exception:
            continue
    return ImageFont.load_default()


def _cutout(image):
    """Subject cut-out via rembg (small u2netp model by default). Returns RGBA or None; never raises."""
    global _SESSION
    if os.getenv("COLLAGE_CUTOUTS", "1").lower() in ("0", "false", "off", "no"):
        return None
    try:
        from rembg import remove, new_session
        if _SESSION is None:
            _SESSION = new_session(os.getenv("COLLAGE_REMBG_MODEL", "u2netp"))
        small = image.copy(); small.thumbnail((640, 640))          # keeps peak RAM low on a 512 MB worker
        raw = io.BytesIO(); small.save(raw, format="PNG")
        out = Image.open(io.BytesIO(remove(raw.getvalue(), session=_SESSION))).convert("RGBA")
        bbox = out.getchannel("A").point(lambda a: 255 if a > 40 else 0).getbbox()
        if not bbox:
            return None
        area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
        if area < 0.08 * out.size[0] * out.size[1]:                # nothing meaningful found
            return None
        return out.crop(bbox)
    except Exception as e:
        log.info("collage cutout skipped: %s", e)
        return None


def _sticker(cut, max_w, max_h):
    """Die-cut look: white outline + soft shadow around the subject."""
    cut = cut.copy(); cut.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
    pad = 18
    base = Image.new("RGBA", (cut.width + pad * 2, cut.height + pad * 2), (0, 0, 0, 0))
    alpha = Image.new("L", base.size, 0); alpha.paste(cut.getchannel("A"), (pad, pad))
    outline = alpha.filter(ImageFilter.MaxFilter(13)).filter(ImageFilter.GaussianBlur(1.2))
    white = Image.new("RGBA", base.size, (255, 255, 255, 255)); white.putalpha(outline)
    shadow = Image.new("RGBA", base.size, (0, 0, 0, 0)); shadow.putalpha(outline.filter(ImageFilter.GaussianBlur(10)).point(lambda a: int(a * .45)))
    out = Image.alpha_composite(shadow, white); out.alpha_composite(cut, (pad, pad))
    return out


def _fit_focus(photo, size, focus):
    """Crop `photo` to size's aspect ratio, keeping the focus point (fx, fy in 0..1) inside the crop."""
    w, h = size; iw, ih = photo.size
    fx, fy = (focus or (0.5, 0.4))[:2]
    scale = max(w / iw, h / ih)
    cw, ch = w / scale, h / scale                                    # crop window in source pixels
    left = min(max(fx * iw - cw / 2, 0), iw - cw)
    top = min(max(fy * ih - ch * 0.42, 0), ih - ch)                  # face sits a bit above the middle
    return photo.crop((int(left), int(top), int(left + cw), int(top + ch))).resize((w, h), Image.Resampling.LANCZOS)


def _torn_card(photo, size, rng, focus=None, torn=True):
    w, h = size; im = _fit_focus(photo, (w, h), focus).convert("RGBA")
    if not torn:
        return im
    mask = Image.new("L", (w, h), 0); d = ImageDraw.Draw(mask)
    pts = []; step = max(10, w // 42)
    for x in range(0, w + step, step): pts.append((min(w, x), rng.randint(0, 7)))
    for y in range(0, h + step, step): pts.append((rng.randint(w - 7, w), min(h, y)))
    for x in range(w, -step, -step): pts.append((max(0, x), rng.randint(h - 7, h)))
    for y in range(h, -step, -step): pts.append((rng.randint(0, 7), max(0, y)))
    d.polygon(pts, fill=255); im.putalpha(mask); return im


def _wrap(draw, text, font, max_w, max_lines=3):
    words, lines, cur = text.split(), [], ""
    for w_ in words:
        t = (cur + " " + w_).strip()
        if draw.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur); cur = w_
    if cur: lines.append(cur)
    return lines[:max_lines]


def render(photos, theme, copy, layout):
    """Dispatch to the style renderer for `theme`; falls back to the generic layout if a style fails."""
    try:
        from .collage_styles import STYLES, render_style
        if theme in STYLES and 3 <= len(photos) <= 5:
            return render_style(photos, theme, copy)
    except Exception:
        log.exception("style renderer failed for %s; using generic layout", theme)
    return render_generic(photos, theme, copy, layout)


def render_generic(photos, theme, copy, layout):
    rng = random.Random(str(theme) + str([p.get("asset_id") for p in photos]))
    bg = layout.get("background", [246, 241, 235]); canvas = Image.new("RGBA", (W, H), tuple(bg) + (255,))
    grain = Image.new("RGBA", (W, H), (0, 0, 0, 0)); gd = ImageDraw.Draw(grain)
    for _ in range(2400):
        gd.point((rng.randrange(W), rng.randrange(H)), fill=(55, 40, 25, rng.randrange(5, 18)))
    canvas = Image.alpha_composite(canvas, grain)
    slots = layout.get("slots", [])[:len(photos)] or [{"x": 80, "y": 240 + i * 300, "w": 900, "h": 270} for i in range(len(photos))]
    messy = theme in ("scrapbook_chaos", "photo_dump", "playful_hype")
    hero = None
    for i, (p, s) in enumerate(zip(photos, slots)):
        photo = ImageOps.exif_transpose(Image.open(io.BytesIO(p["bytes"]))).convert("RGB")
        photo.thumbnail((1600, 1600))
        w, h = int(s["w"]), int(s["h"]); x, y = int(s["x"]), int(s["y"])
        card = _torn_card(photo, (w, h), rng, focus=p.get("focus"), torn=messy)
        backing = Image.new("RGBA", (w + 28, h + 28), (255, 255, 255, 255)); backing.alpha_composite(card, (14, 14))
        backing = backing.rotate(rng.uniform(-5, 5) if messy else rng.uniform(-1.5, 1.5), expand=True, resample=Image.Resampling.BICUBIC)
        x = min(max(x, 8), W - backing.width - 8); y = min(max(y, 8), H - backing.height - 8)   # never clipped by the edge
        shadow = Image.new("RGBA", backing.size, (0, 0, 0, 0)); shadow.putalpha(backing.getchannel("A").filter(ImageFilter.GaussianBlur(13)))
        canvas.alpha_composite(shadow, (x + 8, y + 12)); canvas.alpha_composite(backing, (x, y))
        if i == 0:
            hero = (photo, x, y, backing.width, backing.height)
    draw = ImageDraw.Draw(canvas)
    for i in range(min(10, 3 + len(photos))):
        x = rng.randint(65, W - 90); y = rng.randint(250, H - 260); r = rng.randint(12, 35)
        if i % 3 == 0:
            draw.ellipse((x - r, y - r, x + r, y + r), outline=(35, 35, 35, 210), width=5)
            draw.line((x - r // 2, y, x + r // 2, y), fill=(35, 35, 35, 210), width=4)
        elif i % 3 == 1:
            pts = []
            for k in range(10):
                a = -math.pi / 2 + k * math.pi / 5; rr = r if k % 2 == 0 else r * .42
                pts.append((x + rr * math.cos(a), y + rr * math.sin(a)))
            draw.polygon(pts, fill=(255, 214, 80, 235), outline=(35, 35, 35, 210))
        else:
            draw.arc((x - r, y - r, x + r, y + r), start=15, end=300, fill=(235, 80, 120, 230), width=5)
    if hero:                                                        # one sticker, overlapping the hero card's corner
        cut = _cutout(hero[0])
        if cut is not None:
            st = _sticker(cut, int(hero[3] * .55), int(hero[4] * .95))
            sx = min(hero[1] + hero[3] - int(st.width * .85), W - st.width - 8)
            sy = max(8, hero[2] - int(st.height * .12))
            canvas.alpha_composite(st, (max(8, sx), sy))
    draw = ImageDraw.Draw(canvas)
    colour = tuple(layout.get("text_colour", [25, 25, 25]))
    title = str(copy.get("text", ""))[:110]
    if title:
        size = 66
        while size > 38:
            font = _font(size, True); lines = _wrap(draw, title, font, W - 140)
            if len(lines) <= 2 or size <= 40: break
            size -= 6
        font = _font(size, True); lines = _wrap(draw, title, font, W - 140)
        for k, line in enumerate(lines):
            draw.text((70, 64 + k * int(size * 1.2)), line, font=font, fill=colour + (255,))
    tags = " ".join(copy.get("hashtags", []))[:100]
    if tags:
        draw.text((70, H - 112), tags, font=_font(28), fill=colour + (230,))
    out = io.BytesIO(); canvas.convert("RGB").save(out, "PNG", optimize=True); return out.getvalue()
