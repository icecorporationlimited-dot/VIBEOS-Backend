"""job_handlers.py — the HEAVY code. Imported ONLY by worker.py (inside the
per-job subprocess), never by the web/bot service.

Each handler: handler(job, db) -> JSON-safe dict, or raises (-> retry/fail).
Needs: Pillow, numpy, ffmpeg (+ fonts for stories), optional cv2.
"""
from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import jobs

logger = logging.getLogger("vibeos.handlers")


def _buffer(db):
    from content_buffer import ContentBuffer, GridFSBlobStore
    return ContentBuffer(db["assets"], GridFSBlobStore(db))


def _principal(p: dict):
    from publishing.identity import Principal
    return Principal(p["kind"], p["subject"])


# ------------------------------------------------------------------ edit
def handle_edit(job, db):
    """Free-standing photo edit (web /api/edit, Telegram previews)."""
    from editor import edit_photo
    p = job["payload"]
    data = jobs.get_blob(db, p["in_blob"])
    out, report = edit_photo(
        data, p.get("style", "natural"), p.get("size", "post"), p.get("text") or None,
        p.get("text_style", "minimal"), p.get("text_position", "bottom"), True)
    blob_id = jobs.put_blob(db, out.getvalue(), filename=f"edit_{job['_id'][:8]}.jpg")
    jobs.delete_blob(db, p["in_blob"])
    return {"blob_id": blob_id, "report": {k: str(v)[:60] for k, v in (report or {}).items()}}


# ------------------------------------------------------------------ prepare_ready
def handle_prepare_ready(job, db):
    """selected -> ready: render post/story variants for one asset."""
    from content_buffer import READY
    p = job["payload"]
    buf = _buffer(db)
    asset = buf.assets.find_one({"_id": p["asset_id"]})
    if asset is None:
        raise KeyError(f"unknown asset {p['asset_id']}")
    if asset["status"] == READY:                    # retry after a lost result: already done
        return {"already_ready": True}

    edit_fn = None                                  # None -> adaptive photo engine
    if p.get("style"):
        from editor import edit_photo
        style, overlay, text_style = p["style"], p.get("overlay") or None, p.get("text_style", "minimal")

        def edit_fn(data, _vibe, size):
            return edit_photo(data, style, size, overlay, text_style)

    buf.prepare_ready(p["asset_id"], p.get("caption", ""), p.get("hashtags") or [],
                      p.get("vibe", "natural"), tuple(p.get("sizes") or ("post", "story")), edit_fn)
    return {"asset_id": p["asset_id"]}


# ------------------------------------------------------------------ reel
def handle_reel(job, db):
    """Render a photo Reel (ffmpeg) from the route's story variants; store it as a ready video asset."""
    from creative_studio import reel_engine
    p = job["payload"]
    buf = _buffer(db)
    principal = _principal(p["principal"])
    with tempfile.TemporaryDirectory(prefix="vibeos_reelsrc_") as tmp:
        paths = []
        for n, asset_id in enumerate(p["asset_ids"]):
            path = Path(tmp) / f"{n:02d}.jpg"
            path.write_bytes(buf.get_media(asset_id, "story"))
            paths.append(path)
        audio = p.get("audio_path")
        if audio and not Path(audio).is_file():
            logger.warning("reel audio %s not found on the worker; rendering without audio", audio)
            audio = None
        out = Path(tmp) / "reel.mp4"
        reel_engine.render_photo_reel(paths, out, audio_path=audio)
        meta = reel_engine.probe_video(out)
        data = out.read_bytes()
    res = buf.add_reel(principal, data, meta=meta, caption=p.get("caption", ""),
                       hashtags=p.get("hashtags") or [], vibe=p.get("vibe", "natural"),
                       source_ids=p["asset_ids"], batch_id=p.get("batch_id"))
    return {"reel_id": res["asset"]["_id"], "duplicate": res["duplicate"]}


# ------------------------------------------------------------------ smart_story
def _ai():
    import settings
    from engagement.openrouter_ai import OpenRouterAI
    return OpenRouterAI(getattr(settings, "OPENROUTER_API_KEY", ""),
                        getattr(settings, "OPENROUTER_MODEL", "openai/gpt-4o-mini"))


def handle_smart_story(job, db):
    """verify -> AI plan -> themed render for one trend item (trends.smart_story.build_story)."""
    from trends.smart_story import build_story
    p = job["payload"]
    now = datetime.fromisoformat(p["now"]) if p.get("now") else None
    built = build_story(
        p["item"], ai=_ai(), brand=p.get("brand", "VIBEOS"),
        blocked_topics=p.get("blocked_topics") or [], min_sources=p.get("min_sources", 2),
        min_traffic=p.get("min_traffic", 10_000), allow_solemn=p.get("allow_solemn", True),
        show_sources=p.get("show_sources", True), now=now, variant=p.get("variant", ""))
    blob_id = (jobs.put_blob(db, built["jpg"], filename=f"story_{job['_id'][:8]}.jpg")
               if built.get("jpg") else None)
    return {"ok": built["ok"], "reason": built["reason"], "steps": built["steps"],
            "plan": built["plan"], "blob_id": blob_id}


def handle_story_card(job, db):
    """Non-smart trend card (creative_studio.story_templates.render_trend_story)."""
    from creative_studio.story_templates import render_trend_story
    p = job["payload"]
    with tempfile.TemporaryDirectory(prefix="vibeos_trend_") as tmp:
        path = Path(tmp) / "story.jpg"
        render_trend_story(p["item"], path, brand=p.get("brand", "VIBEOS"),
                           show_headline=p.get("show_headline", True))
        blob_id = jobs.put_blob(db, path.read_bytes(), filename=f"card_{job['_id'][:8]}.jpg")
    return {"blob_id": blob_id}


HANDLERS = {
    "edit": handle_edit,
    "prepare_ready": handle_prepare_ready,
    "reel": handle_reel,
    "smart_story": handle_smart_story,
    "story_card": handle_story_card,
}
