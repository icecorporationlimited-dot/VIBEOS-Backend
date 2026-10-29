"""Renderer: camera-roll energy - the photo IS the story.

Candid crop, film grain, subtle vignette, tilted caption layers, contextual
doodle emojis, optional sticker panel. Never draws a generic gradient.
"""
from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

W, H = 1080, 1920
import asset_hub as hub              # shared asset pools: assets/vibe first, then assets/collage, then fonts/
EMOJI_RE = __import__("re").compile("([\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF][\uFE0F\u200D]*)")

# Style -> candidate chain, resolved through the shared asset hub: an Inter
# upload (Asset Studio, assets/vibe/fonts) upgrades the look the moment it
# lands; Poppins / Lora stay the proven fallback.
_FONT_CANDIDATES = {
    "bold": ("Inter-Bold.ttf", "Inter.ttf", "Poppins-Bold.ttf"),
    "medium": ("Inter-Medium.ttf", "Inter.ttf", "Poppins-Medium.ttf"),
    "light": ("Inter-Light.ttf", "Inter.ttf", "Poppins-Light.ttf"),
    "serif": ("Lora-Italic-Variable.ttf",),
}


@lru_cache(maxsize=16)
def _font(style, size):
    return hub.load_font(_FONT_CANDIDATES.get(style) or _FONT_CANDIDATES["bold"], size)


def _emoji_font_path():
    return hub.emoji_font_path()


@lru_cache(maxsize=128)
def _emoji_img(ch, px):
    path = _emoji_font_path()
    if not path:
        return None
    try:
        font = ImageFont.truetype(path, 109)
        canvas = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
        ImageDraw.Draw(canvas).text((10, 10), ch, font=font, embedded_color=True)
        box = canvas.getbbox()
        if not box:
            return None
        glyph = canvas.crop(box)
        scale = px / max(glyph.size)
        return glyph.resize((max(1, round(glyph.width * scale)), max(1, round(glyph.height * scale))))
    except Exception:
        return None


def _split_emoji(text):
    """(text without emoji, up to 2 emoji) - emojis are pasted as real color
    glyphs, never as tofu boxes in the caption font."""
    emojis = EMOJI_RE.findall(text or "")
    return EMOJI_RE.sub("", text or "").strip(), list(dict.fromkeys(emojis))[:2]


def _cover(photo, faces, zoom=1.0):
    """Face-aware full-bleed 9:16 cover with a candid (slightly off-centre) crop."""
    ratio = max(W / photo.width, H / photo.height) * zoom
    rw, rh = max(W, round(photo.width * ratio)), max(H, round(photo.height * ratio))
    resized = photo.resize((rw, rh), Image.Resampling.LANCZOS)
    left, top = (rw - W) // 2, (rh - H) // 2
    if faces:
        bx = f[0] if False else faces[0]
        cx, cy = (bx[0] + bx[2]) / 2 * rw, (bx[1] + bx[3]) / 2 * rh
        left = max(0, min(rw - W, round(cx - W / 2)))
        top = max(0, min(rh - H, round(cy - H * 0.40)))
    return resized.crop((left, top, left + W, top + H))


def _grain(img, amount):
    # Shared film-grain texture when one is uploaded (assets/vibe/textures),
    # otherwise the proven procedural noise.
    tex = hub.load_image("textures", "grain_soft")
    if tex is not None:
        g = tex.convert("L")
        if g.size != (W, H):
            g = g.resize((W, H), Image.Resampling.LANCZOS)
        img.paste(Image.blend(img, Image.merge("RGB", (g, g, g)),
                              min(0.30, float(amount) * 2)), (0, 0))
        return
    noise = Image.effect_noise((W, H), 28).convert("L")
    img.paste(Image.blend(img, Image.merge("RGB", (noise, noise, noise)), amount), (0, 0))


def _vignette(img):
    tex = hub.load_image("textures", "vignette_soft")
    if tex is not None:                      # uploaded overlay / darkness map
        tex = tex.resize((W, H), Image.Resampling.LANCZOS)
        alpha = tex.getchannel("A") if tex.mode == "RGBA" else None
        if alpha is not None and alpha.getextrema()[0] < 255:
            img.paste(tex.convert("RGB"), (0, 0), alpha)
        else:
            dark = tex.convert("L").point(lambda v: int((255 - v) * 0.45))
            img.paste(Image.new("RGB", (W, H), (0, 0, 0)), (0, 0), dark)
        return
    mask = Image.radial_gradient("L").resize((W, H)).point(lambda v: int(v * 0.45))
    img.paste(Image.new("RGB", (W, H), (0, 0, 0)), (0, 0), ImageOps.invert(mask))


def _wrap(draw, text, font, max_w, max_lines=3):
    words, lines, cur = text.split(), [], ""
    for word in words:
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines[:max_lines]


def render_story(photo, layout, emoji_px_ratio=1.0):
    """photo: PIL RGB image. layout: spec from composition.compose(). Returns RGB image."""
    from vibe_engine.composition import CENTERED, PLACEMENTS
    img = _cover(photo, layout.get("faces") or [], zoom=layout.get("treatment", {}).get("zoom", 1.0))
    t = layout.get("treatment", {})
    if t.get("vignette"):
        _vignette(img)
    if t.get("grain"):
        _grain(img, min(0.10, float(t["grain"])))

    text, cap_emojis = _split_emoji(str(layout.get("caption") or ""))
    if text and not layout.get("no_text"):
        from vibe_engine.personalities import PERSONALITIES
        p = PERSONALITIES[layout["persona"]]
        font = _font(layout.get("font_override") or p["font"], layout["size"])
        if p["lowercase"]:
            text = text[0].lower() + text[1:] if p["font"] == "serif" else text.lower()
        lines = _wrap(ImageDraw.Draw(img), text, font, W - 220, 3)
        line_h = int(font.size * 1.28)
        block_w = max(ImageDraw.Draw(img).textlength(l, font=font) for l in lines)
        block_h = line_h * len(lines)
        ax, ay = PLACEMENTS[layout["placement"]]
        x = (W - block_w) / 2 if ax is None else (ax * W - block_w if ax > 0.5 else ax * W)
        y = ay * H
        layer = Image.new("RGBA", (W, int(block_h) + 120), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        if layout.get("sticker"):
            pad = 46
            d.rounded_rectangle((x - pad, 40, x + block_w + pad, 60 + block_h), radius=28,
                                fill=(255, 255, 255, 235))
            fill = (17, 17, 17)
        else:
            if layout.get("scrim", 90) >= 110:                   # busy photo: soft contrast box
                d.rounded_rectangle((x - 24, 46, x + block_w + 24, 54 + block_h), radius=18,
                                    fill=(0, 0, 0, layout.get("scrim")))
            fill = (255, 255, 255)
            for line in lines:                                  # else: natural soft shadow
                d.text((x + 3, 59 + lines.index(line) * line_h + 3), line, font=font, fill=(0, 0, 0, 160))
        ty = 56
        for line in lines:
            d.text((x, ty), line, font=font, fill=fill)
            ty += line_h
        if cap_emojis:                       # caption emojis as real color glyphs
            lx = x + d.textlength(lines[-1], font=font) + 18
            for em in cap_emojis:
                glyph = _emoji_img(em, int(font.size * 1.15))
                if glyph:
                    layer.paste(glyph, (int(lx), int(ty - line_h - glyph.height // 6)), glyph)
                    lx += glyph.width + 10
        layer = layer.rotate(layout.get("tilt", 0.0), resample=Image.Resampling.BICUBIC,
                            fillcolor=(0, 0, 0, 0))
        img.paste(layer, (0, max(0, int(y - 60))), layer)

    doodles = (layout.get("doodles") or [])[:2]
    if layout.get("no_text") and doodles:                       # emoji-row mode: 3 centered on top
        glyph = _emoji_img(doodles[0], 150)
        if glyph:
            for i in range(3):
                cx = int(W / 2 + (i - 1) * (glyph.width + 70) - glyph.width / 2)
                img.paste(glyph, (cx, 300), glyph)
    for i, emoji in enumerate(doodles):                         # else: doodles hug the caption
        glyph = _emoji_img(emoji, 110)
        if glyph:
            st = glyph.rotate(12 * (i + 1) * (1 if i % 2 == 0 else -1), expand=True,
                              fillcolor=(0, 0, 0, 0))
            ax, ay = PLACEMENTS[layout.get("placement", "bottom_left")]
            x = int(W * (0.82 if ax is None or ax <= 0.5 else 0.18)) - i * 150
            y = int(H * max(0.12, ay - 0.16)) - i * 130
            img.paste(st, (x, y), st)
    return img
