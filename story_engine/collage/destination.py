"""Collage destination: every collage is EITHER an Instagram Story OR a feed Post, never both.

* pick_destination()   random choice (COLLAGE_POST_CHANCE, default 0.5), limited to formats the owner allows
* make_post_jpeg()     9:16 story collage -> 4:5 (1080x1350) feed JPEG (Instagram feed rejects 9:16)
* is_collage_asset()   True for finished collages of either kind (so they are never used as source photos)
"""
import io, os, random

from PIL import Image, ImageFilter, ImageEnhance

STORY, POST = "story", "post"
POST_SIZE = (1080, 1350)                      # 4:5 -> the tallest ratio Instagram accepts in the feed
COLLAGE_KINDS = ("story_collage", "post_collage")


def pick_destination(allowed_formats=None, rng=random):
    """'story' or 'post' at random. allowed_formats = autopilot formats ('story', 'photo', ...); None = both allowed."""
    allowed = set(allowed_formats) if allowed_formats is not None else {"story", "photo"}
    can_story, can_post = "story" in allowed, "photo" in allowed
    if can_story and not can_post:
        return STORY
    if can_post and not can_story:
        return POST
    try:
        chance = min(1.0, max(0.0, float(os.getenv("COLLAGE_POST_CHANCE", "0.5"))))
    except ValueError:
        chance = 0.5
    return POST if rng.random() < chance else STORY


def is_collage_asset(asset):
    return bool(asset) and (asset.get("content_kind") in COLLAGE_KINDS or asset.get("story_only") or asset.get("post_only"))


def make_post_jpeg(png_bytes):
    """Whole collage kept visible (nothing cropped): sharp copy centred on a blurred, softened copy of itself."""
    W, H = POST_SIZE
    with Image.open(io.BytesIO(png_bytes)) as src:
        src = src.convert("RGB")
        scale = max(W / src.width, H / src.height)                       # cover -> blurred backdrop
        bg = src.resize((round(src.width * scale), round(src.height * scale)), Image.Resampling.LANCZOS)
        left, top = (bg.width - W) // 2, (bg.height - H) // 2
        bg = bg.crop((left, top, left + W, top + H)).filter(ImageFilter.GaussianBlur(28))
        bg = ImageEnhance.Brightness(bg).enhance(0.82)
        fg_h = H - 36                                                     # small margin top/bottom
        fg = src.resize((round(src.width * fg_h / src.height), fg_h), Image.Resampling.LANCZOS)
        x, y = (W - fg.width) // 2, (H - fg_h) // 2
        shadow = Image.new("RGBA", (fg.width + 120, fg.height + 120), (0, 0, 0, 0))
        shadow.paste((0, 0, 0, 120), (60, 70, 60 + fg.width, 70 + fg.height))
        shadow = shadow.filter(ImageFilter.GaussianBlur(22))
        canvas = bg.convert("RGBA")
        canvas.alpha_composite(shadow, (x - 60, y - 60))
        canvas.paste(fg, (x, y))
    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="JPEG", quality=92, optimize=True)
    return out.getvalue()
