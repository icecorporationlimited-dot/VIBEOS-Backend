"""Render one sample Story per mood into a contact sheet (layout preview only).

The portrait is a generated placeholder silhouette, NOT a real person's photo.
Usage: python tools/preview_stories.py out_dir
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw  # noqa: E402

from creative_studio.story_themes import render_story  # noqa: E402

SAMPLES = [
    ("solemn", True, "Ek yug ka ant \U0001F54A\uFE0F", "Deepest condolences to the family and fans.", "SHRADDHANJALI"),
    ("celebration", True, "Congratulations champ! \U0001F3C6\U0001F389", "Poora desh aap par garv karta hai \U0001F1EE\U0001F1F3", ""),
    ("festival", True, "Diwali ki dher saari shubhkamnayein \u2728", "Roshni, khushi aur mithai ka tyohaar \U0001FA94", ""),
    ("sports", True, "Kohli ka kamaal, jeet pakki \U0001F525", "Match ke baad poora stadium jhoom utha", ""),
    ("game", False, "New season is live \U0001F47E", "Naye maps, naye skins, same addiction \U0001F3AE", ""),
    ("music", True, "Ye gaana sabki playlist me \U0001F3A7", "Reels pe har jagah wahi beat", ""),
    ("movie", False, "Trailer ne macha diya bawaal \U0001F37F", "Release date ka ab aur intezaar nahi", ""),
    ("tech", False, "New phone launch hype \U0001F4F1", "Camera aur battery ne sabko chauka diya \u26A1", ""),
]


def placeholder_portrait():
    img = Image.new("RGB", (900, 1100), (120, 130, 150))
    d = ImageDraw.Draw(img)
    d.ellipse((300, 220, 600, 560), fill=(200, 170, 150))
    d.ellipse((150, 640, 750, 1300), fill=(60, 70, 100))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


def main(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    photo = placeholder_portrait()
    paths = []
    for kind, with_photo, head, sub, chip in SAMPLES:
        p = render_story({"kind": kind, "headline": head, "subline": sub, "chip": chip,
                          "photo_credit": "Photo: sample / CC BY-SA"},
                         out / f"{kind}.jpg", photo_bytes=photo if with_photo else None,
                         footer="Source: NDTV, Hindustan Times")
        paths.append(p)
    tiles = [Image.open(p).resize((405, 720)) for p in paths]
    sheet = Image.new("RGB", (405 * 4 + 50, 720 * 2 + 30), (30, 30, 30))
    for i, t in enumerate(tiles):
        sheet.paste(t, (10 + (i % 4) * 415, 10 + (i // 4) * 730))
    sheet.save(out / "contact_sheet.jpg", quality=90)
    return out / "contact_sheet.jpg"


if __name__ == "__main__":
    print(main(sys.argv[1] if len(sys.argv) > 1 else "preview_out"))
