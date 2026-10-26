"""Autopilot for collage stories: nobody has to enqueue `collage_story` by hand any more.

Called by publishing.scheduler.Worker (`_auto_collage`) for each autopilot owner. Two steps per owner:
  1. publish_finished(): a collage job that finished -> queue its asset as an Instagram Story (source="auto",
     so the normal policy gates apply: quiet hours, daily limit, min gap, /pause, blocked topics)
  2. maybe_enqueue():    enough scheduled photos + nothing recent/open -> enqueue a new `collage_story` media job

Each collage is randomly published as a Story OR a feed Post (never both): destination is picked when the job is
enqueued (destination.pick_destination), stored on the job and used again when it is queued for publishing.

Env: COLLAGE_AUTO=on|off (default on), COLLAGE_AUTO_EVERY_HOURS=24, COLLAGE_MIN_PHOTOS=4,
     COLLAGE_POST_CHANCE=0.5 (probability of Post; 0 = always Story, 1 = always Post)
"""
import logging, os
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)
FAIL_BACKOFF_HOURS = 6


def enabled():
    return os.getenv("COLLAGE_AUTO", "on").strip().lower() not in ("0", "off", "false", "no")


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _owned(principal):
    return {"payload.principal.kind": principal.kind, "payload.principal.subject": principal.subject}


def eligible_photo_count(db, principal) -> int:
    from publishing.identity import owner_filter
    ids = set()
    rows = list(db["publish_jobs"].find({**owner_filter(principal), "format": {"$in": ["photo", "story", "carousel"]},
                                         "status": {"$in": ["queued", "retry", "held"]},
                                         "scheduled_at": {"$ne": None}}))
    for pj in rows[:60]:
        ids.update(str(a) for a in pj.get("asset_ids", []) or [])
    if not ids:
        return 0
    return db["assets"].count_documents({"_id": {"$in": list(ids)}, "status": "ready", "kind": "photo",
                                         "story_only": {"$ne": True}, "post_only": {"$ne": True}, **owner_filter(principal)})


def maybe_enqueue(db, principal, now=None, formats=None):
    """Enqueue one collage job if the owner has enough photos and none ran/queued recently. Returns job id or None."""
    import jobs
    now = now or _now()
    hours = float(os.getenv("COLLAGE_AUTO_EVERY_HOURS", "24"))
    last = db[jobs.COLLECTION].find_one({"type": "collage_story", **_owned(principal)}, sort=[("created", -1)])
    if last:
        wait = FAIL_BACKOFF_HOURS if last.get("status") == jobs.FAILED else hours
        if last.get("status") in (jobs.QUEUED, jobs.RUNNING) or _aware(last["created"]) > now - timedelta(hours=wait):
            log.info("collage autopilot: %s waiting (last job %s, status %s)", principal.owner_id, last.get("_id", "")[:8], last.get("status"))
            return None
    have, need = eligible_photo_count(db, principal), int(os.getenv("COLLAGE_MIN_PHOTOS", "4"))
    if have < need:
        log.info("collage autopilot: %s has %d scheduled ready photo(s), needs %d", principal.owner_id, have, need)
        return None
    from .destination import pick_destination
    destination = pick_destination(formats)
    log.info("collage autopilot: %s -> new collage will be a %s", principal.owner_id, destination.upper())
    return jobs.enqueue(db, "collage_story", {"principal": {"kind": principal.kind, "subject": principal.subject},
                                              "destination": destination})


def publish_finished(db, service, principal):
    """Queue finished collages as a Story or a Post, whichever they were made for (once). Returns [asset_id]."""
    import jobs
    from publishing.service import PublishError
    queued = []
    done = list(db[jobs.COLLECTION].find({"type": "collage_story", "status": jobs.DONE, "story_queued": {"$ne": True},
                                          **_owned(principal)}))
    for j in sorted(done, key=lambda r: r["created"])[:3]:
        asset_id = (j.get("result") or {}).get("asset_id")
        if not asset_id:
            db[jobs.COLLECTION].update_one({"_id": j["_id"]}, {"$set": {"story_queued": True}})
            continue
        dest = (j.get("result") or {}).get("destination") or (j.get("payload") or {}).get("destination") or "story"
        fmt = "photo" if dest == "post" else "story"
        try:
            pub = service.enqueue_for(principal, [asset_id], fmt, scheduled_at=None, source="auto")
        except PublishError as exc:                       # not connected / asset gone: stop retrying this one
            log.warning("collage %s not queued: %s", asset_id, exc)
            db[jobs.COLLECTION].update_one({"_id": j["_id"]}, {"$set": {"story_queued": True, "story_error": str(exc)[:200]}})
            continue
        db[jobs.COLLECTION].update_one({"_id": j["_id"]}, {"$set": {"story_queued": True, "publish_job_id": pub.get("_id"), "published_as": fmt}})
        queued.append(asset_id)
    return queued


def tick(db, service, principal, now=None, formats=None):
    """One owner, one pass. formats = owner's allowed autopilot formats. Returns {"queued": [...], "enqueued": job_id|None}."""
    out = {"queued": publish_finished(db, service, principal), "enqueued": None}
    out["enqueued"] = maybe_enqueue(db, principal, now, formats)
    return out
