"""Mood-based 9:16 Story renderer (replaces the single dark template).

One design per mood so a tragedy never looks like a celebration:

  solemn      pure black, B&W portrait, serif text, one quiet emoji (condolence)
  celebration confetti on a vivid gradient, framed photo, party emoji
  festival    warm saffron/maroon, bokeh lights, arch-shaped photo
  sports      navy + neon stripes, big photo with the headline over it
  game        neon grid / pixel frame, glitch-style title
  music       magenta gradient, equaliser bars, vinyl-style round photo
  movie       cinematic teal/orange, gold frame
  tech        blue circuit dots, cyan accent
  general     clean gradient fallback

Text may contain colour emoji (Noto Color Emoji) and Hinglish. Devanagari needs
libraqm for correct shaping; without it we refuse to draw it rather than
render broken glyphs (the AI planner is told to write Roman script anyway).

Everything that must be read stays inside Instagram's safe zone (y 270..1530).
"""
from __future__ import annotations

import hashlib
import io
import random
import re
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps, features

W, H = 1080, 1920
import asset_hub as hub              # shared asset pools: assets/vibe first, then assets/collage, then fonts/
DEVA_RE = re.compile(r"[\u0900-\u097F]")
EMOJI_RE = re.compile(
    "([\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF]"
    "[\uFE0F\u200D\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]*)")

THEMES = {
    "solemn": dict(grad=((0, 0, 0), (0, 0, 0)), text=(245, 245, 245), sub=(170, 170, 170),
                   accent=(255, 255, 255), style="serif", shape="circle", painter=None),
    "celebration": dict(grad=((255, 61, 129), (255, 168, 76)), text=(255, 255, 255), sub=(255, 240, 230),
                        accent=(255, 255, 255), style="bold", shape="round", painter="confetti",
                        chip="LET'S GO \U0001F389", chip_fill=(255, 255, 255), chip_text=(200, 30, 90),
                        hero="\U0001F389"),
    "festival": dict(grad=((70, 8, 30), (255, 132, 20)), text=(255, 226, 140), sub=(255, 240, 210),
                     accent=(255, 200, 80), style="bold", shape="arch", painter="bokeh",
                     chip="HAPPY VIBES \U0001FA94", chip_fill=(255, 200, 80), chip_text=(80, 20, 20),
                     hero="\U0001FA94"),
    "sports": dict(grad=((6, 10, 30), (14, 40, 90)), text=(255, 255, 255), sub=(200, 225, 255),
                   accent=(0, 232, 150), style="bold", shape="round", painter="stripes",
                   chip="MATCH DAY \u26A1", chip_fill=(0, 232, 150), chip_text=(5, 20, 20),
                   hero="\U0001F3CF", upper=True),
    "game": dict(grad=((16, 6, 44), (60, 10, 90)), text=(120, 255, 255), sub=(235, 200, 255),
                 accent=(255, 60, 200), style="bold", shape="pixel", painter="grid",
                 chip="GAMER MOMENT \U0001F3AE", chip_fill=(255, 60, 200), chip_text=(255, 255, 255),
                 hero="\U0001F3AE", upper=True),
    "music": dict(grad=((40, 0, 80), (255, 0, 110)), text=(255, 255, 255), sub=(255, 225, 240),
                  accent=(255, 255, 255), style="bold", shape="circle", painter="bars",
                  chip="ON REPEAT \U0001F3B5", chip_fill=(255, 255, 255), chip_text=(120, 0, 90),
                  hero="\U0001F3A4"),
    "movie": dict(grad=((8, 28, 36), (140, 70, 20)), text=(255, 225, 150), sub=(240, 230, 210),
                  accent=(240, 190, 90), style="bold", shape="round", painter="dots",
                  chip="WATCHLIST \U0001F3AC", chip_fill=(240, 190, 90), chip_text=(30, 20, 10),
                  hero="\U0001F3AC"),
    "tech": dict(grad=((4, 14, 40), (0, 70, 130)), text=(255, 255, 255), sub=(190, 225, 255),
                 accent=(0, 210, 255), style="bold", shape="round", painter="dots",
                 chip="TECH STUFF \U0001F916", chip_fill=(0, 210, 255), chip_text=(0, 25, 50),
                 hero="\U0001F4F1"),
    "general": dict(grad=((60, 40, 160), (20, 130, 200)), text=(255, 255, 255), sub=(225, 235, 255),
                    accent=(255, 255, 255), style="bold", shape="round", painter="dots",
                    chip="TRENDING RN \U0001F525", chip_fill=(255, 255, 255), chip_text=(40, 30, 120),
                    hero="\U0001F525"),
}
THEMES["congrats"] = THEMES["celebration"]
KINDS = tuple(THEMES)

# 2-3 palettes per mood (seeded pick) so two Stories of the same kind never look identical.
GRADS = {
    "solemn": [((0, 0, 0), (0, 0, 0))],
    "celebration": [((255, 61, 129), (255, 168, 76)), ((255, 94, 58), (255, 45, 149)),
                    ((120, 0, 200), (255, 105, 180))],
    "festival": [((70, 8, 30), (255, 132, 20)), ((122, 8, 60), (255, 92, 40)),
                 ((60, 30, 5), (255, 170, 60))],
    "sports": [((6, 10, 30), (14, 40, 90)), ((0, 22, 34), (0, 96, 84)), ((10, 5, 40), (84, 20, 104))],
    "game": [((16, 6, 44), (60, 10, 90)), ((4, 24, 40), (0, 120, 130)), ((30, 0, 60), (120, 10, 60))],
    "music": [((40, 0, 80), (255, 0, 110)), ((0, 60, 120), (170, 0, 200)), ((20, 0, 70), (0, 160, 170))],
    "movie": [((8, 28, 36), (140, 70, 20)), ((24, 12, 40), (90, 30, 90)), ((5, 30, 24), (120, 90, 20))],
    "tech": [((4, 14, 40), (0, 70, 130)), ((10, 0, 50), (60, 0, 130)), ((0, 30, 40), (0, 110, 100))],
    "general": [((60, 40, 160), (20, 130, 200)), ((240, 90, 40), (120, 20, 140)), ((20, 0, 60), (0, 150, 120))],
}
GRADS["congrats"] = GRADS["celebration"]


# ---------------------------------------------------------------- fonts / emoji
def _first_existing(paths):
    return next((p for p in paths if Path(p).exists()), None)


def has_emoji_font() -> bool:
    return hub.emoji_font_path() is not None


def raqm_ok() -> bool:
    return bool(features.check("raqm"))


# Style -> candidate chain, resolved through the shared asset hub: an Inter
# upload (assets/vibe/fonts) upgrades the look the moment it lands; Poppins /
# Lora stay the proven fallback.
_FONT_CANDIDATES = {
    "bold": ("Inter-Bold.ttf", "Inter.ttf", "Poppins-Bold.ttf"),
    "medium": ("Inter-Medium.ttf", "Inter.ttf", "Poppins-Medium.ttf"),
    "light": ("Inter-Light.ttf", "Inter.ttf", "Poppins-Light.ttf"),
    "serif": ("Lora-Italic-Variable.ttf",),
}


@lru_cache(maxsize=64)
def _font(style: str, size: int):
    return hub.load_font(_FONT_CANDIDATES.get(style) or _FONT_CANDIDATES["bold"], size)


def pick_font(style: str, size: int, text: str = ""):
    if DEVA_RE.search(text):
        if not raqm_ok():
            raise ValueError("Devanagari text needs libraqm (apt install libraqm0); use Roman Hinglish")
        style = "bold" if style == "serif" else style
    return _font(style, size)


@lru_cache(maxsize=256)
def _emoji_img(ch: str, px: int):
    path = hub.emoji_font_path()
    if not path:
        return None
    try:
        font = ImageFont.truetype(path, 109)             # the only size CBDT bitmaps allow
        canvas = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
        ImageDraw.Draw(canvas).text((10, 10), ch, font=font, embedded_color=True)
        box = canvas.getbbox()
        if not box:
            return None
        glyph = canvas.crop(box)
        scale = px / max(glyph.size)
        return glyph.resize((max(1, round(glyph.width * scale)), max(1, round(glyph.height * scale))),
                            Image.Resampling.LANCZOS)
    except (OSError, ValueError):
        return None


def _segments(text: str):
    return [s for s in EMOJI_RE.split(text) if s]


def _is_emoji(seg: str) -> bool:
    return bool(EMOJI_RE.fullmatch(seg))


def measure(draw, text, font, emoji_px) -> float:
    total = 0.0
    for seg in _segments(text):
        if _is_emoji(seg):
            glyph = _emoji_img(seg, emoji_px)
            total += (glyph.width + 8) if glyph else 0
        else:
            total += draw.textlength(seg, font=font)
    return total


def draw_rich(img, draw, x, y, text, font, fill, emoji_px, shadow=None):
    """Draw text with inline colour emoji. (x, y) is the top-left of the line."""
    cx = x
    for seg in _segments(text):
        if _is_emoji(seg):
            glyph = _emoji_img(seg, emoji_px)
            if glyph:
                img.paste(glyph, (round(cx), round(y + (font.size - glyph.height) / 2 + 4)), glyph)
                cx += glyph.width + 8
        else:
            if shadow:
                draw.text((cx + 3, y + 4), seg, font=font, fill=shadow)
            draw.text((cx, y), seg, font=font, fill=fill)
            cx += draw.textlength(seg, font=font)


def wrap_fit(draw, text, style, start, floor, max_w, max_lines):
    """Largest font size (start..floor) whose wrap fits max_lines. Returns (font, lines)."""
    size = start
    while True:
        font = pick_font(style, size, text)
        words, lines, cur = text.split(), [], ""
        for word in words:
            trial = (cur + " " + word).strip()
            if measure(draw, trial, font, int(size * 0.95)) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        if len(lines) <= max_lines or size <= floor:
            return font, lines[:max_lines]
        size -= 4


def draw_block(img, draw, lines, font, fill, y, line_h, *, center=True, shadow=None, x=100, spacing=0):
    emoji_px = int(font.size * 0.95)
    for line in lines:
        width = measure(draw, line, font, emoji_px)
        left = (W - width) / 2 if center else x
        draw_rich(img, draw, left, y, line, font, fill, emoji_px, shadow)
        y += line_h
    return y


# ---------------------------------------------------------------- layouts
LAYOUTS = ("photo_hero", "text_only", "meme_reaction", "handwritten")


def _pick_layout(plan, kind, has_photo):
    """Story form: the plan decides (AI), with a safe default per kind; text_only without a photo."""
    layout = str(plan.get("layout") or "").strip().lower()
    if kind == "solemn" or layout not in LAYOUTS:
        layout = "photo_hero" if has_photo else "text_only"
    if not has_photo and layout != "text_only":
        layout = "text_only"
    return layout


def _sticker_text(img, lines, font, fill, y):
    """Handwritten-style caption: drawn on its own layer, tilted like a sticker."""
    emoji_px = int(font.size * 0.95)
    layer = Image.new("RGBA", (W, 460), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cy = 60
    for line in lines:
        draw_rich(layer, d, 140, cy, line, font, fill, emoji_px, shadow=(0, 0, 0, 70))
        cy += int(font.size * 1.3)
    layer = layer.rotate(-2.5, resample=Image.Resampling.BICUBIC, fillcolor=(0, 0, 0, 0))
    img.paste(layer, (0, y - 40), layer)
    return y + cy + 30


# ---------------------------------------------------------------- backgrounds
def _gradient(top, bottom):
    img = Image.new("RGB", (W, H))
    px = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        px.line((0, y, W, y), fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return img


def _rng(seed_text):
    return random.Random(int(hashlib.sha1(seed_text.encode("utf-8")).hexdigest()[:12], 16))


def _paint(img, kind, theme, seed):
    painter, rnd = theme.get("painter"), _rng(seed)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if painter == "confetti":
        palette = [(255, 255, 255), (255, 232, 90), (90, 220, 255), (150, 255, 160), (255, 120, 220)]
        for _ in range(110):
            x, y, s = rnd.randrange(W), rnd.randrange(H), rnd.randrange(14, 34)
            color = rnd.choice(palette) + (rnd.randrange(150, 235),)
            if rnd.random() < 0.5:
                d.ellipse((x, y, x + s // 2, y + s // 2), fill=color)
            else:
                piece = Image.new("RGBA", (s, s // 2), color).rotate(rnd.randrange(360), expand=True)
                layer.paste(piece, (x, y), piece)
    elif painter == "bokeh":
        for _ in range(55):
            x, y, r = rnd.randrange(W), rnd.randrange(H), rnd.randrange(20, 90)
            d.ellipse((x - r, y - r, x + r, y + r), fill=(255, rnd.randrange(170, 230), 90, rnd.randrange(25, 70)))
        layer = layer.filter(ImageFilter.GaussianBlur(6))
    elif painter == "stripes":
        for i in range(-2, 8):
            x = i * 260
            d.polygon([(x, 0), (x + 90, 0), (x + 90 + 700, H), (x + 700, H)], fill=theme["accent"] + (22,))
    elif painter == "grid":
        for x in range(0, W, 60):
            d.line((x, 0, x, H), fill=(0, 255, 255, 28), width=2)
        for y in range(0, H, 60):
            d.line((0, y, W, y), fill=(255, 60, 200, 28), width=2)
        for y in range(0, H, 6):
            d.line((0, y, W, y), fill=(0, 0, 0, 40))        # scanlines
    elif painter == "bars":
        for i in range(24):
            h = rnd.randrange(80, 330)
            x = 60 + i * 41
            d.rounded_rectangle((x, 1530 - h, x + 26, 1530), radius=10, fill=(255, 255, 255, 70))
    elif painter == "dots":
        for gx in range(40, W, 70):
            for gy in range(40, H, 70):
                if rnd.random() < 0.35:
                    d.ellipse((gx - 3, gy - 3, gx + 3, gy + 3), fill=theme["accent"] + (70,))
    img.paste(layer, (0, 0), layer)


# ---------------------------------------------------------------- photo shapes
_YUNET = {"det": None, "tried": False}
_YUNET_FILE = "face_detection_yunet_2023mar.onnx"


def _yunet():
    """Lazy optional YuNet (same ONNX the collage worker uses). None if cv2/model missing."""
    if _YUNET["tried"]:
        return _YUNET["det"]
    _YUNET["tried"] = True
    try:
        import cv2
        root = Path(__file__).resolve().parents[1]
        path = next((str(p) for p in (root / "models" / _YUNET_FILE, Path("/app/models") / _YUNET_FILE)
                     if p.is_file() and p.stat().st_size > 100_000), None)
        if path:
            _YUNET["det"] = cv2.FaceDetectorYN.create(path, "", (320, 320), 0.6, 0.3, 5000)
    except Exception:
        pass
    return _YUNET["det"]


def _face_box(photo: Image.Image):
    """Union of detected faces as normalised [x0, y0, x1, y1], or None (soft: never raises)."""
    det = _yunet()
    if det is None:
        return None
    try:
        import cv2
        import numpy as np
        small = photo.copy()
        small.thumbnail((640, 640))
        w, h = small.size
        bgr = cv2.cvtColor(np.array(small.convert("RGB")), cv2.COLOR_RGB2BGR)
        det.setInputSize((w, h))
        _, rows = det.detect(bgr)
        if rows is None or not len(rows):
            return None
        boxes = [(max(x, 0) / w, max(y, 0) / h, min(x + fw, w) / w, min(y + fh, h) / h)
                 for x, y, fw, fh in ((float(v) for v in r[:4]) for r in rows) if fw >= 10 and fh >= 10]
        if not boxes:
            return None
        return [min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes)]
    except Exception:
        return None


def _cover(photo: Image.Image, w, h, top_bias=0.25, face=None):
    ratio = max(w / photo.width, h / photo.height)
    resized = photo.resize((max(w, round(photo.width * ratio)), max(h, round(photo.height * ratio))),
                           Image.Resampling.LANCZOS)
    left, top = (resized.width - w) // 2, round((resized.height - h) * top_bias)
    if face:                                       # keep the face union inside the tile
        rw, rh = resized.width, resized.height
        cx, cy = (face[0] + face[2]) / 2 * rw, (face[1] + face[3]) / 2 * rh
        left = max(0, min(rw - w, round(cx - w / 2)))
        top = round(cy - h * 0.42)                 # face slightly above centre
        top = max(int(face[1] * rh - 0.10 * h), top)      # keep headroom above
        top = min(int(face[3] * rh + 0.10 * h) - h, top)   # ...and room below
        top = max(0, min(rh - h, top))
    return resized.crop((left, top, left + w, top + h))


def _mask(shape, w, h):
    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    if shape == "circle":
        d.ellipse((0, 0, w - 1, h - 1), fill=255)
    elif shape == "arch":
        d.pieslice((0, 0, w - 1, w - 1), 180, 360, fill=255)          # round top
        d.rounded_rectangle((0, w // 2, w - 1, h - 1), radius=40, fill=255)
        d.rectangle((0, w // 2, w - 1, w // 2 + 60), fill=255)
    elif shape == "pixel":
        d.rectangle((16, 16, w - 17, h - 17), fill=255)
    else:
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=48, fill=255)
    return m


def _place_photo(img, kind, theme, photo_bytes):
    """Returns the y where text can start."""
    shape = theme["shape"]
    size = {"circle": (600, 600), "arch": (600, 700), "pixel": (760, 640)}.get(shape, (800, 640))
    x, y = (W - size[0]) // 2, 400
    try:
        photo = ImageOps.exif_transpose(Image.open(io.BytesIO(photo_bytes))).convert("RGB")
    except Exception as exc:
        raise ValueError("photo is not a readable image") from exc
    tile = _cover(photo, *size, face=_face_box(photo))
    if kind == "solemn":
        tile = ImageOps.grayscale(tile)
        tile = ImageOps.autocontrast(tile, cutoff=1).convert("RGB")
        vig = Image.radial_gradient("L").resize(size)       # dark edges, quiet look
        tile = Image.composite(tile, Image.new("RGB", size, (0, 0, 0)), ImageOps.invert(vig).point(lambda v: 255 - int(v * 0.55)))
    mask = _mask(shape, *size)
    glow = Image.new("RGBA", (size[0] + 120, size[1] + 120), (0, 0, 0, 0))
    gc = theme["accent"] + ((45 if kind == "solemn" else 110),)
    glow.paste(Image.new("RGBA", size, gc), (60, 60), mask)
    glow = glow.filter(ImageFilter.GaussianBlur(28))
    img.paste(glow, (x - 60, y - 60), glow)
    img.paste(tile, (x, y), mask)
    if shape in ("round", "arch", "circle") and kind != "solemn":
        ring = Image.new("RGBA", size, (0, 0, 0, 0))
        edge = mask.filter(ImageFilter.MaxFilter(13))
        ring.paste(Image.new("RGBA", size, theme["accent"] + (255,)), (0, 0), ImageChops.subtract(edge, mask))
        img.paste(ring, (x, y), ring)
    if shape == "pixel":
        d = ImageDraw.Draw(img)
        for i, col in enumerate(((255, 60, 200), (0, 255, 255))):
            d.rectangle((x + i * 6, y + i * 6, x + size[0] - i * 6, y + size[1] - i * 6), outline=col, width=6)
    return y + size[1] + 60


def _place_hero_emoji(img, kind, theme, plan):
    emojis = [e for e in (plan.get("emojis") or []) if _emoji_img(e, 40)] or [theme.get("hero", "\U0001F54A\uFE0F")]
    big = _emoji_img(emojis[0], 360 if kind != "solemn" else 300)
    cy = 640
    if big:
        img.paste(big, ((W - big.width) // 2, cy - big.height // 2), big)
    for i, e in enumerate(emojis[1:3]):
        small = _emoji_img(e, 130)
        if small:
            x = 150 if i == 0 else W - 150 - small.width
            img.paste(small, (x, cy + 190 + i * 40), small)
    return 1070


# ---------------------------------------------------------------- main entry
def _chip(img, draw, theme, text, y=300):
    font = pick_font("bold", 34, text)
    px = 30
    width = measure(draw, text, font, 34) + 2 * 40
    x = (W - width) / 2
    draw.rounded_rectangle((x, y, x + width, y + 72), radius=36, fill=theme["chip_fill"])
    draw_rich(img, draw, x + 40, y + 12, text, font, theme["chip_text"], 34)


def render_story(plan: dict, output_path, *, photo_bytes: bytes | None = None, brand: str = "VIBEOS",
                 footer: str = "", seed: str = "") -> Path:
    """Render one Story from an AI/user plan:
       {kind, headline, subline, chip, emojis[], photo_credit}. Raises ValueError on bad input."""
    kind = str(plan.get("kind", "general")).lower()
    theme = THEMES.get(kind)
    headline = str(plan.get("headline", "")).strip()
    if not theme or not headline:
        raise ValueError("plan needs a known kind and a headline")
    if not has_emoji_font():
        plan = {**plan, "emojis": []}
        headline = EMOJI_RE.sub("", headline).strip()
    subline = str(plan.get("subline", "")).strip()
    # (all-caps headlines removed: stories read casual, not shouty)

    grads = GRADS.get(kind) or [theme["grad"]]
    img = _gradient(*grads[_rng(headline + str(seed)).randrange(len(grads))])
    _paint(img, kind, theme, headline + str(seed))
    draw = ImageDraw.Draw(img)

    if kind == "solemn":
        label = str(plan.get("chip") or "SHRADDHANJALI").upper()
        font = pick_font("medium", 30, label)
        spaced = " ".join(label)
        draw.text(((W - draw.textlength(spaced, font=font)) / 2, 310), spaced, font=font, fill=(150, 150, 150))
    layout = _pick_layout(plan, kind, photo_bytes is not None)
    ty = _place_photo(img, kind, theme, photo_bytes) if photo_bytes else _place_hero_emoji(img, kind, theme, plan)
    draw = ImageDraw.Draw(img)
    shadow = None if kind == "solemn" else (0, 0, 0, 90)
    style = theme["style"]
    if kind == "solemn":
        font, lines = wrap_fit(draw, headline, style, 88 if photo_bytes else 96, 54, W - 200, 3 if not photo_bytes else 2)
        y = draw_block(img, draw, lines, font, theme["text"], ty, int(font.size * 1.22), shadow=shadow)
    elif layout == "photo_hero" and photo_bytes:
        font, lines = wrap_fit(draw, headline, "medium", 58, 38, W - 220, 2)      # small, natural, left-aligned
        y = draw_block(img, draw, lines, font, theme["text"], ty, int(font.size * 1.35),
                       center=False, x=110, shadow=shadow)
    elif layout == "meme_reaction" and photo_bytes:
        y0 = min(max(ty + 40, 620), H - 460)
        band = Image.new("RGBA", (W, 340), (0, 0, 0, 130))                        # meme-style reaction strip
        img.paste(band, (0, y0), band)
        draw = ImageDraw.Draw(img)
        font, lines = wrap_fit(draw, headline, "bold", 96, 62, W - 160, 2)
        y = draw_block(img, draw, lines, font, (255, 255, 255), y0 + 50, int(font.size * 1.16))
    elif layout == "handwritten" and photo_bytes:
        font, lines = wrap_fit(draw, headline, "serif", 56, 36, W - 280, 2)        # Lora italic = handwritten vibe
        y = _sticker_text(img, lines, font, theme["text"], ty)
    else:                                                                         # text_only / no layout match
        font, lines = wrap_fit(draw, headline, style, 96 if not photo_bytes else 88, 54, W - 200, 3 if not photo_bytes else 2)
        y = draw_block(img, draw, lines, font, theme["text"], ty, int(font.size * 1.22), shadow=shadow)
    if kind == "solemn":
        draw.line((W / 2 - 60, y + 14, W / 2 + 60, y + 14), fill=(120, 120, 120), width=2)
        y += 40
    if subline:
        sfont, slines = wrap_fit(draw, subline, "light" if kind == "solemn" else "medium", 40, 30, W - 220, 2)
        draw_block(img, draw, slines, sfont, theme["sub"], y + 14, int(sfont.size * 1.4))

    credit = str(plan.get("photo_credit") or "").strip()
    foot = credit[:90] if (photo_bytes and credit) else ""           # news-source labels: removed
    if foot:
        ffont = pick_font("light", 24, foot)
        draw.text(((W - draw.textlength(foot, font=ffont)) / 2, 1478), foot, font=ffont,
                  fill=(140, 140, 140) if kind == "solemn" else theme["sub"])
    bfont = pick_font("bold", 26, brand)
    mark = brand[:18].upper()
    draw.text(((W - draw.textlength(mark, font=bfont)) / 2, 1510), mark, font=bfont,
              fill=(110, 110, 110) if kind == "solemn" else theme["sub"])

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out, "JPEG", quality=92, optimize=True)
    return out
