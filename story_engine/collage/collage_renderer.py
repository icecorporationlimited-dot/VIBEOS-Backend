"""Layered 1080x1920 story collage: paper grain, face-aware photo cards, one die-cut subject sticker, doodles.

Rendering quality: anti-aliased 2x torn edges (fibre rim + ragged photo edge), 2-layer warm shadow, gaussian grain and
the paper texture come from collage_fx.py (this file is the generic FALLBACK; the six styles live in collage_styles.py).

Fixes vs the first version:
  * crops keep the whole `subject_box` (YuNet face -> rembg mask -> saliency) inside, with headroom above faces;
    if the subject cannot fit a slot's ratio the photo is shown whole (contain) instead of being cut (subject_crop.py)
  * only the hero photo gets a cutout sticker (before, every photo was pasted twice and looked doubled)
  * cutouts really work: rembg is optional, small model, input capped, any failure just skips the sticker
  * title wraps to the canvas instead of running off the right edge; project Poppins font is used
"""
from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageFilter
from pathlib import Path
import io, os, random, math, logging
from . import collage_fx as fx

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


def rembg_session():
    """Shared rembg session (small u2netp model). None when rembg is not installed or cut-outs are disabled."""
    global _SESSION
    if os.getenv("COLLAGE_CUTOUTS", "1").lower() in ("0", "false", "off", "no"):
        return None
    try:
        if _SESSION is None:
            from rembg import new_session
            _SESSION = new_session(os.getenv("COLLAGE_REMBG_MODEL", "u2netp"))
        return _SESSION
    except Exception as e:
        log.info("rembg unavailable: %s", e)
        return None


def _cutout(image):
    """Subject cut-out via rembg (small u2netp model by default). Returns RGBA or None; never raises."""
    global _SESSION
    if os.getenv("COLLAGE_CUTOUTS", "1").lower() in ("0", "false", "off", "no"):
        return None
    from . import budget
    if not budget.have(35):                                        # cutouts are decoration: skip when late
        log.info("collage cutout skipped: %.0fs budget left", budget.remaining())
        return None
    try:
        from rembg import remove, new_session
        if _SESSION is None:
            _SESSION = new_session(os.getenv("COLLAGE_REMBG_MODEL", "u2netp"))
        small = image.copy(); small.thumbnail((384, 384))          # keeps peak RAM low on a 512 MB worker
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
    """Die-cut look: smooth white outline (built at 2x, anti-aliased) + two-layer soft shadow around the subject."""
    cut = cut.copy(); cut.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
    pad = 24
    base = Image.new("RGBA", (cut.width + pad * 2, cut.height + pad * 2), (0, 0, 0, 0))
    alpha = Image.new("L", base.size, 0); alpha.paste(cut.getchannel("A"), (pad, pad))
    ss = fx.SS
    outline = fx.grow(alpha.resize((base.width * ss, base.height * ss), Image.Resampling.BICUBIC), 9 * ss).resize(base.size, Image.Resampling.LANCZOS)
    white = Image.new("RGBA", base.size, (255, 255, 255, 255)); white.putalpha(outline)
    out = white; out.alpha_composite(cut, (pad, pad))
    return out


def _fit_focus(photo, size, subject):
    """Photo -> exactly `size`. Keeps subject_box / face_box fully inside the crop (12 % headroom above faces, no extra
    zoom); if they cannot fit the slot ratio the whole photo is shown over a blurred copy. `subject` is the dict from
    photo_understanding.locate_subject (a photo dict is accepted too). Use fit_slot() to also get the crop report."""
    return fit_slot(photo, size, subject)[0]


def fit_slot(photo, size, subject):
    from .subject_crop import render_slot, subject_of
    return render_slot(photo, size, subject_of(subject))


def _torn_card(photo, size, rng, subject=None, torn=True):
    w, h = size; im = _fit_focus(photo, (w, h), subject).convert("RGBA")
    if torn:
        im.putalpha(fx.torn_mask((w, h), rng, amp=6, wavelength=60))
    return im


def _torn_card_info(photo, size, rng, subject=None, torn=True):
    """Like _torn_card but also returns the crop report ({"mode": "crop" | "contain"})."""
    from .subject_crop import render_slot, subject_of
    im, info = render_slot(photo, size, subject_of(subject))
    im = im.convert("RGBA")
    if torn:
        im.putalpha(fx.torn_mask(size, rng, amp=6, wavelength=60))
    return im, info


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


def render(photos, theme, copy, layout, report=None):
    """Dispatch to the style renderer for `theme`; falls back to the generic layout if a style fails.
    `report` (optional dict) receives report["plan"]: the slot plan the validator checks (safe zone, gaps, ratios)."""
    try:
        from .collage_styles import STYLES, render_style
        if theme in STYLES and 3 <= len(photos) <= 5:
            return render_style(photos, theme, copy, report)
    except Exception:
        log.exception("style renderer failed for %s; using generic layout", theme)
    return render_generic(photos, theme, copy, layout, report)


def render_generic(photos, theme, copy, layout, report=None):
    """Fallback look. Slots come from the layout planner (orientation-matched fixed ratios, >= 24 px gaps, nothing in the
    top / bottom 250 px); layout["slots"] from the theme JSON is no longer used, only its colours."""
    from .collage_layout_planner import plan_slots, junction_points, SAFE_Y1
    rng = random.Random(str(theme) + str([p.get("asset_id") for p in photos]))
    bg = layout.get("background", [246, 241, 235]); canvas = fx.paper_bg(tuple(bg), "fine")
    plan = plan_slots(photos, caption=190)
    messy = theme in ("scrapbook_chaos", "photo_dump", "playful_hype")
    for p, s in zip(plan["photos"], plan["slots"]):
        photo = ImageOps.exif_transpose(Image.open(io.BytesIO(p["bytes"]))).convert("RGB")
        photo.thumbnail((1600, 1600))
        w, h = s["w"], s["h"]; b = 16
        tile, info = fit_slot(photo, (w - 2 * b, h - 2 * b), p.get("subject_region"))
        s["mode"] = info.get("mode", "crop")
        card = fx.torn_card(tile, rng, border=b, amp=6, angle=rng.uniform(-3, 3) if messy else rng.uniform(-1, 1), torn=messy)
        if card.width > w or card.height > h:                        # a tilted card never leaves its slot (no accidental overlap)
            card.thumbnail((w, h), Image.Resampling.LANCZOS)
        x, y = s["x"] + (w - card.width) // 2, s["y"] + (h - card.height) // 2
        fx.place(canvas, card, x, y)                                 # contact + ambient warm multiply shadow
    draw = ImageDraw.Draw(canvas)
    for i, (x, y) in enumerate(junction_points(plan, rng, 6)):      # doodles only in the gaps between photos
        r = rng.randint(12, 20)
        if i % 3 == 0:
            draw.ellipse((x - r, y - r, x + r, y + r), outline=(35, 35, 35, 210), width=4)
            draw.line((x - r // 2, y, x + r // 2, y), fill=(35, 35, 35, 210), width=3)
        elif i % 3 == 1:
            pts = []
            for k in range(10):
                a = -math.pi / 2 + k * math.pi / 5; rr = r if k % 2 == 0 else r * .42
                pts.append((x + rr * math.cos(a), y + rr * math.sin(a)))
            draw.polygon(pts, fill=(255, 214, 80, 235), outline=(35, 35, 35, 210))
        else:
            draw.arc((x - r, y - r, x + r, y + r), start=15, end=300, fill=(235, 80, 120, 230), width=4)
    colour = tuple(layout.get("text_colour", [25, 25, 25]))
    top = plan["caption_top"]
    title = str(copy.get("text", ""))[:110]
    y = top
    if title:
        size = 58
        while size > 36:
            font = _font(size, True); lines = _wrap(draw, title, font, W - 120)
            if len(lines) <= 2: break
            size -= 4
        font = _font(size, True); lines = _wrap(draw, title, font, W - 120)[:2]
        for line in lines:
            draw.text((60, y), line, font=font, fill=colour + (255,)); y += int(size * 1.2)
    tags = " ".join(copy.get("hashtags", []))[:100]
    if tags:
        draw.text((60, min(y + 8, SAFE_Y1 - 36)), tags, font=_font(28), fill=colour + (230,))
    if report is not None:
        report["plan"] = {k: v for k, v in plan.items() if k != "photos"}
    canvas = fx.grain(canvas, rng, 0.05)                              # gaussian grain, not sparse dots
    out = io.BytesIO(); canvas.convert("RGB").save(out, "PNG", optimize=True); return out.getvalue()
