"""collage_story job handler (runs inside worker.py). Moved out of job_handlers.py so that file stays small."""
import io
from datetime import datetime, timezone

MAX_CANDIDATES = 12      # photos loaded into RAM per collage (nearest scheduled first)


def handle_collage_story(job, db):
    """Pick real scheduled photos, render a separate story-only collage asset."""
    import jobs
    from PIL import Image
    from content_buffer import ContentBuffer, GridFSBlobStore, READY
    from publishing.identity import Principal, owner_filter as principal_owner_filter, principal_from_owner_id
    from .collage_selector import select_photos
    from .collage_brain import choose_theme, trend_context
    from .collage_copywriter import write_copy
    from .collage_layouts import load_layout
    from .collage_renderer import render
    from .collage_validator import validate
    from . import budget
    budget.reset()

    p = job["payload"]
    raw_principal = p.get("principal")
    if raw_principal:
        principal = Principal(raw_principal["kind"], raw_principal["subject"])
    else:
        principal = principal_from_owner_id(str(p.get("owner_id", ""))) or Principal.from_telegram(str(p.get("owner_id", "")))
    owner_filter = principal_owner_filter(principal)
    asset_stamp = {"owner_id": principal.owner_id}
    if principal.kind == "telegram":
        asset_stamp["telegram_id"] = principal.subject
    now = datetime.now(timezone.utc)

    scheduled_jobs = list(db["publish_jobs"].find({**owner_filter, "format": {"$in": ["photo", "story", "carousel"]},
        "status": {"$in": ["queued", "retry", "held"]}, "scheduled_at": {"$ne": None}}).sort("scheduled_at", 1).limit(60))
    discovered = {}
    for pj in scheduled_jobs:
        for aid in pj.get("asset_ids", []) or []:
            discovered.setdefault(str(aid), pj.get("scheduled_at"))
    for aid in p.get("asset_ids", [])[:30]:
        discovered.setdefault(str(aid), None)

    buf = ContentBuffer(db["assets"], GridFSBlobStore(db))
    rows = []
    for aid, sched in discovered.items():
        if len(rows) >= MAX_CANDIDATES:
            break
        asset = db["assets"].find_one({"_id": aid, **owner_filter})
        if not asset or asset.get("kind", "photo") != "photo" or asset.get("status") != READY:
            continue
        if asset.get("story_only") or asset.get("content_kind") == "story_collage":
            continue                                       # never build a collage out of an earlier collage
        try:
            raw = buf.get_media(aid)
        except Exception:
            continue
        creative = asset.get("creative") or {}; meta = asset.get("metadata") or {}; collage_meta = asset.get("collage_meta") or {}
        rows.append({"asset_id": aid, "blob_id": asset.get("blob_id"), "status": "ready", "scheduled_at": sched,
                     "bytes": raw, "filename": asset.get("filename"), "source": asset.get("source"),
                     "caption": creative.get("caption") or asset.get("caption") or "",
                     "tags": creative.get("tags") or meta.get("tags") or asset.get("tags") or [],
                     "description": meta.get("description") or asset.get("description") or "",
                     "category": creative.get("category") or meta.get("category") or asset.get("category") or "",
                     "location": meta.get("location") or asset.get("location") or "",
                     "outfit": meta.get("outfit") or asset.get("outfit") or "",
                     "event": meta.get("event") or asset.get("event") or ""})

    budget.log.info("collage: loaded %d candidate photos in %.1fs", len(rows), budget.elapsed())
    with budget.stage("select"):
        photos = select_photos(rows, 5)
    del rows
    if len(photos) < 3:
        raise ValueError("Need 3+ owned, ready photos linked to real scheduled jobs (or explicit asset IDs).")
    theme, category = choose_theme(photos, p.get("theme"))
    with budget.stage("trends"):
        trend = trend_context(category)
    with budget.stage("copy"):
        copy = write_copy(category, theme, trend)
    with budget.stage("render"):
        png = render(photos, theme, copy, load_layout(theme, len(photos)))
    check = validate(png, copy, len(photos))
    if not check["valid"]:
        raise ValueError("Collage validation failed: " + ",".join(check["errors"]))

    png_blob = buf.blobs.put(png, filename=f"story_collage_{job['_id'][:8]}.png")
    with Image.open(io.BytesIO(png)) as im:
        jpg = io.BytesIO(); im.convert("RGB").save(jpg, format="JPEG", quality=92, optimize=True)
    jpg_blob = buf.blobs.put(jpg.getvalue(), filename=f"story_collage_{job['_id'][:8]}.jpg")
    asset_id = "collage_" + job["_id"]
    source_ids = [x["asset_id"] for x in photos]
    doc = {"_id": asset_id, **asset_stamp, "kind": "photo", "source": "story_collage",
           "content_kind": "story_collage", "story_only": True, "feed_eligible": False, "post_eligible": False,
           "status": "ready", "blob_id": png_blob, "size_bytes": len(png), "sha256": None,
           "creative": {"caption": copy.get("text", ""), "hashtags": copy.get("hashtags", []), "vibe": theme,
                        "variants": {"story": jpg_blob}},
           "collage_meta": {"theme": theme, "source_asset_ids": source_ids, "trend_context": trend, "validation": check,
                            "photo_selection": [{"asset_id": x.get("asset_id"), "role": x.get("selection_role"),
                                                 "score": x.get("selection_score"),
                                                 "reasons": x.get("selection_reasons", [])} for x in photos]},
           "created_at": now, "updated_at": now}
    db["assets"].insert_one(doc)
    return {"asset_id": asset_id, "png_blob_id": png_blob, "theme": theme, "source_asset_ids": source_ids,
            "copy": copy, "trend_context": trend, "validation": check, "content_type": "story_collage",
            "destination": "story", "feed_eligible": False, "post_eligible": False}
