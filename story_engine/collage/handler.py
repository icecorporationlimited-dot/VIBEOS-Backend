"""collage_story job handler (runs inside worker.py). payload.destination = \"story\" | \"post\" decides the output:\nStory -> 1080x1920 story variant only, Post -> 1080x1350 (4:5) post variant only."""
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
    from .destination import POST, is_collage_asset, make_post_jpeg
    budget.reset()

    p = job["payload"]
    destination = POST if str(p.get("destination", "story")).lower() in ("post", "photo", "feed") else "story"
    is_post = destination == POST
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
        if is_collage_asset(asset):
            continue                                       # never build a collage out of an earlier collage
        try:
            raw = buf.get_media(aid)
        except Exception:
            continue
        rows.append({"asset_id": aid, "blob_id": asset.get("blob_id"), "status": "ready", "scheduled_at": sched,
                     "bytes": raw, "filename": asset.get("filename"), "source": asset.get("source")})

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
        report = {}
        png = render(photos, theme, copy, load_layout(theme, len(photos)), report)
    check = validate(png, copy, len(photos), report.get("plan"), destination)
    if not check["valid"]:
        raise ValueError("Collage validation failed: " + ",".join(check["errors"]))

    kind = "post_collage" if is_post else "story_collage"
    png_blob = buf.blobs.put(png, filename=f"{kind}_{job['_id'][:8]}.png")
    if is_post:                                            # 4:5 feed JPEG; no story variant, so it cannot be a Story
        out_blob = buf.blobs.put(make_post_jpeg(png), filename=f"{kind}_{job['_id'][:8]}.jpg")
    else:                                                  # 9:16 story JPEG; no post variant, so it cannot be a Post
        with Image.open(io.BytesIO(png)) as im:
            jpg = io.BytesIO(); im.convert("RGB").save(jpg, format="JPEG", quality=92, optimize=True)
        out_blob = buf.blobs.put(jpg.getvalue(), filename=f"{kind}_{job['_id'][:8]}.jpg")
    asset_id = "collage_" + job["_id"]
    source_ids = [x["asset_id"] for x in photos]
    doc = {"_id": asset_id, **asset_stamp, "kind": "photo", "source": "story_collage",
           "content_kind": kind, "destination": destination,
           "story_only": not is_post, "post_only": is_post, "feed_eligible": is_post, "post_eligible": is_post,
           "status": "ready", "blob_id": png_blob, "size_bytes": len(png), "sha256": None,
           "creative": {"caption": copy.get("text", ""), "hashtags": copy.get("hashtags", []), "vibe": theme,
                        "variants": {("post" if is_post else "story"): out_blob}},
           "collage_meta": {"theme": theme, "source_asset_ids": source_ids, "trend_context": trend, "validation": check},
           "created_at": now, "updated_at": now}
    db["assets"].insert_one(doc)
    return {"asset_id": asset_id, "png_blob_id": png_blob, "theme": theme, "source_asset_ids": source_ids,
            "copy": copy, "trend_context": trend, "validation": check, "content_type": kind,
            "destination": destination, "feed_eligible": is_post, "post_eligible": is_post}
