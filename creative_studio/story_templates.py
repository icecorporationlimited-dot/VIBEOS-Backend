"""Generate a simple 9:16 branded Story draft from sourced trend metadata.

This renders a local image only. It does not publish, and it intentionally
labels source/context so a trending search term is not presented as verified fact.
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920


def _font(path: str | None, size: int, bold=False):
    candidates = [path] if path else []
    candidates += [
        str(Path(__file__).resolve().parents[1] / "fonts" / ("Poppins-Bold.ttf" if bold else "Poppins-Medium.ttf")),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                pass
    return ImageFont.load_default()


REGION_LABELS = {"IN": "India", "US": "the US", "GB": "the UK", "CA": "Canada",
                 "AU": "Australia", "AE": "the UAE", "SG": "Singapore"}


def _wrap(draw, text, font, max_w, max_lines):
    """Wrap by pixel width; the last line gets an ellipsis if text was cut."""
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
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and draw.textlength(lines[-1] + "...", font=font) > max_w:
            lines[-1] = lines[-1][:-1].rstrip()
        lines[-1] += "..."
    return lines


def render_trend_story(item: dict, output_path: str | Path, *, brand: str = "VIBEOS",
                       accent: tuple[int, int, int] = (126, 91, 255), font_path: str | None = None,
                       show_headline: bool = True) -> Path:
    """Render a 1080x1920 Story card.

    Shows only what the source gave us: the search term, approximate demand, and
    (optionally) the top publisher headline with its publisher name. Nothing is
    paraphrased or claimed; the footer says this is a search trend, not verified news.
    """
    title = str(item.get("title", "")).strip()
    url = str(item.get("source_url", "")).strip()
    if not title or not url.startswith("https://"):
        raise ValueError("A title and HTTPS source URL are required for a trend Story draft")
    news = [n for n in (item.get("news") or []) if n.get("title") and n.get("source")]
    evidence = item.get("evidence") or {}
    traffic = str(evidence.get("approx_traffic") or "").strip()
    region = REGION_LABELS.get(str(evidence.get("region", "")).upper())
    publishers = []
    for n in news:
        if n["source"] not in publishers:
            publishers.append(n["source"])

    # Instagram covers ~14% at the top (profile bar) and ~20% at the bottom (reply
    # box): everything that must be read stays between y=270 and y=1530.
    img = Image.new("RGB", (W, H), (13, 15, 25))
    draw = ImageDraw.Draw(img)
    for y in range(H):
        blend = y / H
        draw.line((0, y, W, y), fill=(int(16 + 12 * blend), int(18 + 5 * blend), int(35 + 18 * blend)))
    draw.rounded_rectangle((70, 90, W - 70, H - 90), radius=44, outline=(68, 70, 100), width=3)
    draw.rounded_rectangle((100, 285, 415, 360), radius=30, fill=accent)
    draw.text((125, 302), "TRENDING NOW", font=_font(font_path, 30, True), fill="white")
    lead = f"People in {region} are searching" if region else "People are searching"
    draw.text((105, 395), lead, font=_font(font_path, 44, False), fill=(200, 201, 219))

    inner_w = W - 200
    title_font = _font(font_path, 80, True)
    y = 470
    for line in _wrap(draw, title, title_font, inner_w, 3):
        draw.text((100, y), line, font=title_font, fill=(255, 255, 255))
        y += 100
    y += 34
    if traffic:
        chip = f"{traffic} searches"
        chip_font = _font(font_path, 32, True)
        cw = int(draw.textlength(chip, font=chip_font)) + 56
        draw.rounded_rectangle((100, y, 100 + cw, y + 66), radius=33, fill=(29, 31, 49),
                               outline=(68, 70, 100), width=2)
        draw.text((128, y + 14), chip, font=chip_font, fill=(223, 223, 235))
        y += 66
    y += 56

    if news and show_headline:
        top = news[0]
        head_font, src_font = _font(font_path, 42, False), _font(font_path, 30, True)
        hl = _wrap(draw, top["title"], head_font, inner_w - 70, 3)
        box_h = 76 + len(hl) * 58 + 70
        box_top = min(y, 1340 - box_h)
        draw.rounded_rectangle((100, box_top, W - 100, box_top + box_h), radius=28,
                               fill=(29, 31, 49), outline=(68, 70, 100), width=2)
        draw.text((135, box_top + 28), "IN THE NEWS", font=_font(font_path, 24, True), fill=accent)
        ty = box_top + 76
        for line in hl:
            draw.text((135, ty), line, font=head_font, fill=(235, 235, 245))
            ty += 58
        draw.text((135, ty + 8), "- " + top["source"][:40], font=src_font, fill=(180, 182, 205))
    else:
        draw.text((105, min(y, 1300)), "Trend signal - details are being verified.",
                  font=_font(font_path, 31), fill=(223, 223, 235))

    draw.text((105, 1390), "SOURCE", font=_font(font_path, 25, True), fill=accent)
    src_line = "Google Trends" + (" / " + ", ".join(publishers[:3]) if publishers else "")
    draw.text((105, 1432), src_line[:60], font=_font(font_path, 30), fill=(230, 230, 240))
    draw.text((105, 1490), "Search trend, not verified news.", font=_font(font_path, 27), fill=(160, 163, 184))
    draw.text((W - 245, 1490), brand[:18].upper(), font=_font(font_path, 27, True), fill=(230, 230, 240))

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "JPEG", quality=92, optimize=True)
    return out
