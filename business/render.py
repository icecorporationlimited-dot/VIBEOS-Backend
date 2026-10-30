"""business/render.py - Phase 4 branded-card renderer (runs in the worker).

Turns the Creative agent's visual spec (layout + overlay text + brand colors)
into a real JPEG sized for Instagram:

    feed  -> 1080x1350 (4:5; inside Instagram's 0.8-1.91 photo ratio)
    story -> 1080x1920 (9:16; inside Instagram's 0.5-0.6 story ratio)

Only real facts are drawn: the business name, the QC-approved overlay text and
the brand colors. No fake logos, photos, prices or claims. Fonts resolve
through the shared asset hub, so any checkout renders (system fallback when
no fonts are uploaded).
"""
from __future__ import annotations

import io
import re

from PIL import Image, ImageDraw

SIZES = {"feed": (1080, 1350), "story": (1080, 1920)}
LAYOUTS = ("brand_card", "quote_card", "product_promo", "simple_overlay")
DEFAULT_BG = "#20222E"


def _rgb(value, default):
    m = re.match(r"^#?([0-9a-fA-F]{6})$", str(value or "").strip())
    if not m:
        return default
    h = m.group(1)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _luminance(rgb):
    r, g, b = rgb
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def _font(size, weight=None):
    import asset_hub
    return asset_hub.load_font(("Inter.ttf", "Poppins.ttf", "Poppins-SemiBold.ttf"),
                               size, weight)


def _wrap(draw, text, font, max_width, max_lines=4):
    words, lines, line = str(text or "").split(), [], ""
    for word in words:
        cand = (line + " " + word).strip()
        if not line or draw.textlength(cand, font=font) <= max_width:
            line = cand
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines[:max_lines]


def _gradient(size, top, bottom):
    w, h = size
    base = Image.new("RGB", (1, max(h, 2)))
    for y in range(h):
        t = y / max(h - 1, 1)
        base.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t)
                                    for i in range(3)))
    return base.resize(size)


def render_card(content, workspace, fmt="feed") -> bytes:
    """JPEG bytes for one QC-passed business content document."""
    if fmt not in SIZES:
        raise ValueError("unsupported card format " + str(fmt))
    size = SIZES[fmt]
    visual = content.get("visual") or {}
    layout = visual.get("layout")
    if layout not in LAYOUTS:
        layout = "brand_card"
    palette = [c for c in ((workspace.get("brand") or {}).get("colors") or [])
               if re.match(r"^#[0-9a-fA-F]{6}$", str(c))][:3]
    bg_top = _rgb(palette[0] if palette else None, _rgb(DEFAULT_BG, (32, 34, 46)))
    bg_bottom = _rgb(palette[1] if len(palette) > 1 else None, bg_top)
    ink = (18, 18, 24) if _luminance(bg_top) > 0.62 else (255, 255, 255)
    accent = _rgb(palette[-1] if palette else None, (214, 41, 118))
    if accent == bg_top:
        accent = ink

    img = _gradient(size, bg_top, bg_bottom)
    draw = ImageDraw.Draw(img)
    w, h = size
    name = str(workspace.get("name") or "").strip()
    overlay = str(visual.get("overlay_text") or "").strip()

    name_font = _font(int(w * 0.042), weight=600)
    body_font = _font(int(w * 0.072), weight=500)

    # business name: top bar for most layouts, footer for quote cards
    if name:
        if layout == "quote_card":
            draw.text((int(w * 0.09), h - int(h * 0.10)), name,
                      font=name_font, fill=ink)
        else:
            draw.text((int(w * 0.09), int(h * 0.07)), name,
                      font=name_font, fill=ink)
            bar_y = int(h * 0.07) + int(w * 0.055)
            draw.rectangle([int(w * 0.09), bar_y,
                            int(w * 0.09) + int(w * 0.12), bar_y + 8], fill=accent)

    # overlay text (the QC-approved message)
    if overlay:
        lines = _wrap(draw, overlay, body_font, w - int(w * 0.18))
        line_h = int(w * 0.09)
        total = line_h * len(lines)
        y = (h - total) // 2
        for i, line in enumerate(lines):
            if layout in ("brand_card", "product_promo"):
                x = int(w * 0.09)
            else:
                x = max(0, (w - draw.textlength(line, font=body_font)) // 2)
            draw.text((x, y + i * line_h), line, font=body_font, fill=ink)

    out = io.BytesIO()
    img.save(out, "JPEG", quality=92)
    return out.getvalue()
