"""Step 8 — Render. asset (copy + image + layout) -> 1080x1920 PNG via headless Chromium -> R2/S3 -> rendered_image_url.

Does NOT mark anything "ready": that is step 9 (quality check). Render only records `render_meta`
(did text overflow? did the image load?) so step 9 has real measurements instead of guesses.
Renderer and uploader are injectable so tests run without Chromium or a bucket.
"""
import logging
import os
import uuid
from datetime import datetime, timezone

from . import config as cfg
from .db import ASSETS, LAYOUTS
from .layouts import TEMPLATES_DIR, build_html

log = logging.getLogger(__name__)

# measured inside the page after fonts + image finished loading
_MEASURE = """() => {
  const c = document.querySelector('.copy'), card = document.querySelector('.card');
  const cs = getComputedStyle(card), pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
  return {text_height: c.scrollHeight, room: card.clientHeight - pad,
          overflow: c.scrollHeight > card.clientHeight - pad || c.scrollWidth > c.clientWidth + 1}
}"""


class PlaywrightRenderer:
    """One Chromium for a whole batch (launching per story is slow). Use as a context manager."""

    def __enter__(self):
        from playwright.sync_api import sync_playwright          # lazy: only the worker that renders needs it
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(args=["--no-sandbox"])
        return self

    def __exit__(self, *a):
        self._browser.close()
        self._pw.stop()

    def render(self, html_text, template_dir) -> tuple:
        """-> (png_bytes, meta). HTML is written next to style.css so relative css/font paths resolve."""
        path = os.path.join(template_dir, f"_render_{uuid.uuid4().hex}.html")
        w, h = cfg.RENDER_SIZE
        page = self._browser.new_page(viewport={"width": w, "height": h})
        try:
            open(path, "w", encoding="utf-8").write(html_text)
            page.goto("file://" + os.path.abspath(path), wait_until="networkidle", timeout=cfg.RENDER_TIMEOUT_MS)
            page.evaluate("document.fonts.ready")
            meta = page.evaluate(_MEASURE)
            png = page.screenshot(type="png", clip={"x": 0, "y": 0, "width": w, "height": h})
            return png, meta
        finally:
            page.close()
            if os.path.exists(path):
                os.remove(path)


class R2Uploader:
    def __init__(self):
        import boto3
        self._s3 = boto3.client("s3", endpoint_url=cfg.R2_ENDPOINT or None,
                                aws_access_key_id=cfg.R2_ACCESS_KEY_ID, aws_secret_access_key=cfg.R2_SECRET_ACCESS_KEY,
                                region_name="auto")

    def upload(self, key, data, content_type="image/png") -> str:
        self._s3.put_object(Bucket=cfg.R2_BUCKET, Key=key, Body=data, ContentType=content_type,
                            CacheControl="public, max-age=31536000, immutable")
        return f"{cfg.R2_PUBLIC_BASE.rstrip('/')}/{key}"


def object_key(asset) -> str:
    # layout_version is part of the key: re-rendering after a template change never overwrites an old URL
    return f"stories/{asset['trend_id']}/{asset['_id']}-{asset['layout_id']}-v{asset['layout_version']}.png"


def process(db, renderer, uploader, limit=20, base=TEMPLATES_DIR) -> dict:
    """Render every asset that has copy + image + layout but no PNG yet."""
    stats = {"rendered": 0, "failed": 0}
    rows = db[ASSETS].find({"layout_id": {"$ne": None}, "image": {"$ne": None},
                            "rendered_image_url": None, "quality_status": "pending"})
    for a in list(rows)[:limit]:
        layout = db[LAYOUTS].find_one({"layout_id": a["layout_id"], "version": a["layout_version"]}) \
            or db[LAYOUTS].find_one({"layout_id": a["layout_id"]})
        try:
            tdir = os.path.join(base, os.path.basename(layout["template_path"]))
            png, meta = renderer.render(build_html(layout, a, base), tdir)
            url = uploader.upload(object_key(a), png)
        except Exception:
            log.exception("render failed for asset %s", a.get("_id"))   # one bad story never stops the batch
            stats["failed"] += 1
            continue
        db[ASSETS].update_one({"_id": a["_id"]}, {"$set": {
            "rendered_image_url": url, "render_meta": meta, "rendered_at": datetime.now(timezone.utc)}})
        stats["rendered"] += 1
    return stats
