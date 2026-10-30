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


# ------------------------------------------------------------------ automatic output framing
def _choose_output_size(image_bytes: bytes) -> str:
    """Choose one Instagram format from the source aspect ratio; no user choice needed."""
    from PIL import Image, ImageOps
    import io
    with Image.open(io.BytesIO(image_bytes)) as source:
        image = ImageOps.exif_transpose(source)
        width, height = image.size
    if not width or not height:
        return "post"
    ratio = width / height
    if ratio < 0.645:             # already close to a 9:16 story frame
        return "story"
    if 0.94 <= ratio <= 1.06:     # preserve near-square compositions
        return "square"
    return "post"                 # default polished 4:5 Instagram feed canvas


# ------------------------------------------------------------------ tagger content (faces / scene tags)
def _clean_tagger_faces(raw) -> list:
    """Tagger faces -> [[x, y, w, h], ...] in 0..1; anything malformed is dropped."""
    out = []
    for f in (raw or [])[:8]:
        try:
            x, y, w, h = (float(v) for v in f[:4])
        except Exception:
            continue
        if 0 <= x < 1 and 0 <= y < 1 and 0.01 < w <= 1 and 0.01 < h <= 1:
            out.append([round(x, 4), round(y, 4), round(w, 4), round(h, 4)])
    return out


def _clean_tagger_tags(raw) -> list:
    return [str(t)[:24] for t in (raw or []) if isinstance(t, str)][:8]


def _take_detected(payload: dict, diagnostic: dict):
    """(tags, faces) for edit_photo. Caller-supplied values win; otherwise the ones the
    tagger found. Also removes them from the diagnostic so they never reach the report."""
    tags = diagnostic.pop("_tags", None)
    faces = diagnostic.pop("_faces", None)
    return (payload.get("tags") or tags or None), (payload.get("faces") or faces or None)


# ------------------------------------------------------------------ automatic style decision
def _choose_automatic_style(image_bytes: bytes) -> tuple[str, dict]:
    """Choose one internal style. Tagger failure safely falls back to editor auto mode."""
    diagnostic = {"style_source": "builtin_fallback"}
    try:
        from tagger_client import tag_photo
        from media_names import resolve_style
        tagged = tag_photo(image_bytes)
        if tagged:
            # Faces + scene words are useful even when the style itself is low-confidence:
            # they keep the framing face-safe and let the editor retouch skin.
            diagnostic["_faces"] = _clean_tagger_faces(tagged.get("faces"))
            diagnostic["_tags"] = _clean_tagger_tags(tagged.get("tags"))
            confidence = int(tagged.get("style_confidence") or 0)
            chosen = resolve_style(tagged.get("style"), "natural")
            diagnostic.update({
                "style_source": "tagger" if chosen != "natural" else "tagger_fallback",
                "style_confidence": confidence,
                "style_label": str(tagged.get("style_label") or "")[:80],
                "style_low_confidence": bool(tagged.get("low_confidence", True)),
            })
            if chosen != "natural" and not tagged.get("low_confidence", True) and confidence >= 45:
                diagnostic["style_id"] = chosen
                if isinstance(tagged.get("edit"), dict):
                    diagnostic["tagger_edit_plan"] = tagged["edit"]
                    diagnostic["adaptive_plan"] = tagged["edit"]
                return chosen, diagnostic
    except Exception:
        logger.warning("Automatic style tagger unavailable; using built-in auto selection", exc_info=True)
    diagnostic["style_id"] = "natural"
    return "natural", diagnostic


# ------------------------------------------------------------------ edit
def handle_edit(job, db):
    """Automatic photo edit: internally choose a look and return one final image."""
    from editor import edit_photo
    p = job["payload"]
    data = jobs.get_blob(db, p["in_blob"])
    text = (p.get("text") or None) if p.get("text_required") else None

    # Consumer uploads default to automatic style selection. Trusted internal
    # callers may set force_style=True for a deliberate non-user-facing render.
    if p.get("force_style"):
        style = p.get("style", "natural")
        style_diagnostic = {"style_source": "internal_override", "style_id": style}
    else:
        style, style_diagnostic = _choose_automatic_style(data)

    output_size = _choose_output_size(data) if p.get("size", "auto") == "auto" else p.get("size", "post")
    tags, faces = _take_detected(p, style_diagnostic)
    out, report = edit_photo(
        data, style, output_size, text,
        p.get("text_style", "minimal"), p.get("text_position", "bottom"), True,
        tags=tags, faces=faces, seed=p.get("seed"),
        adaptive_plan=style_diagnostic.get("adaptive_plan"))
    report = dict(report or {})
    report.update(style_diagnostic)
    report["output_size"] = output_size
    blob_id = jobs.put_blob(db, out.getvalue(), filename=f"edit_{job['_id'][:8]}.jpg")
    jobs.delete_blob(db, p["in_blob"])
    return {"blob_id": blob_id, "report": {k: str(v)[:160] for k, v in report.items()}}


# ------------------------------------------------------------------ prepare_ready
def handle_prepare_ready(job, db):
    """selected -> ready: render post/story variants with an automatic per-photo look."""
    from content_buffer import READY
    from editor import edit_photo
    p = job["payload"]
    buf = _buffer(db)
    asset = buf.assets.find_one({"_id": p["asset_id"]})
    if asset is None:
        raise KeyError(f"unknown asset {p['asset_id']}")
    if asset["status"] == READY:
        return {"already_ready": True}

    # Choose once from the original, then reuse the same look for every output size.
    original = buf.get_media(p["asset_id"])
    if p.get("force_style"):
        style = p.get("style", "natural")
        style_diagnostic = {"style_source": "internal_override", "style_id": style}
    else:
        style, style_diagnostic = _choose_automatic_style(original)

    overlay = (p.get("overlay") or None) if p.get("text_required") else None
    tags, faces = _take_detected(p, style_diagnostic)
    seed = p.get("seed") or p["asset_id"]

    def edit_fn(data, _vibe, size):
        rendered, _report = edit_photo(
            data, style, size, overlay, p.get("text_style", "minimal"),
            p.get("text_position", "bottom"), True,
            tags=tags, faces=faces, seed=seed,
            adaptive_plan=style_diagnostic.get("adaptive_plan"))
        return rendered

    buf.prepare_ready(p["asset_id"], p.get("caption", ""), p.get("hashtags") or [],
                      p.get("vibe", "natural"), tuple(p.get("sizes") or ("post", "story")), edit_fn)
    return {"asset_id": p["asset_id"], "style": style,
            "style_source": style_diagnostic.get("style_source", "builtin_fallback")}


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
    from engagement.openrouter_ai import build_llm
    return build_llm(settings)


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


def handle_collage_story(job, db):
    """Story-only photo collage; implementation lives in story_engine/collage/handler.py."""
    from story_engine.collage.handler import handle_collage_story as _run
    return _run(job, db)


# ------------------------------------------------------------------ business_content
def handle_business_content(job, db):
    """Business Mode content factory for one campaign plan item (business.content).

    Copywriting -> Creative -> QC agents; output goes to business_content_assets.
    Runs on Business Mode's own Groq key (GROQ_BUSINESS_API_KEY).
    """
    from business.content import ContentFactory
    from business.store import BusinessStore
    p = job["payload"]
    factory = ContentFactory(BusinessStore(db))
    return factory.run_job(p["workspace_id"], p["campaign_id"], p["item_id"], job["_id"])


HANDLERS = {
    "edit": handle_edit,
    "prepare_ready": handle_prepare_ready,
    "reel": handle_reel,
    "smart_story": handle_smart_story,
    "story_card": handle_story_card,
    "collage_story": handle_collage_story,
    "business_content": handle_business_content,
}
