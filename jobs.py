"""jobs.py — tiny Mongo-backed job queue for heavy media work.

Light on purpose: only pymongo + stdlib, so the web/bot service can import it
without pulling in Pillow/numpy/cv2/ffmpeg. The heavy code lives in
job_handlers.py and only runs inside worker.py (Render Background Worker).

Collection `media_jobs`:
    type         "prepare_ready" | "edit" | "reel" | "smart_story" | "story_card"
    asset_id     asset this job is about (None for jobs without an asset)
    status       queued -> running -> done | failed   (running with an expired
                 lease is reclaimed by the next worker)
    lease_until  datetime; a running job whose lease expired is claimable again
    attempts     incremented on every claim; 3 failed attempts -> failed
    created      datetime
    payload      JSON-safe input
    result       JSON-safe output (blob ids, never raw bytes)
    error        last error text

Index: (status, type, created)
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

COLLECTION = "media_jobs"
QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
MAX_ATTEMPTS = 3
DEFAULT_LEASE_SECONDS = 360          # longer than the slowest job (reel: 300 s ffmpeg timeout)
JOB_TTL_DAYS = 2                     # finished job rows are removed after this

TYPES = ("prepare_ready", "edit", "reel", "smart_story", "story_card", "collage_story",
          "business_content", "business_render")


class JobFailed(RuntimeError):
    """The job reached status=failed (after all attempts) or its handler gave up."""


class JobTimeout(TimeoutError):
    """The worker did not finish the job in time (it may still run later)."""


def _now():
    return datetime.now(timezone.utc)


def ensure_indexes(db):
    col = db[COLLECTION]
    col.create_index([("status", 1), ("type", 1), ("created", 1)])
    # Finished jobs clean themselves up; `finished_at` is only set on done/failed.
    col.create_index([("finished_at", 1)], expireAfterSeconds=JOB_TTL_DAYS * 86400,
                     sparse=True)


# ---------------------------------------------------------------- producer side
def enqueue(db, job_type, payload=None, asset_id=None) -> str:
    """Insert a queued job and return its id. Never does any heavy work."""
    if job_type not in TYPES:
        raise ValueError(f"unknown job type {job_type!r}")
    job_id = uuid.uuid4().hex
    db[COLLECTION].insert_one({
        "_id": job_id,
        "type": job_type,
        "asset_id": asset_id,
        "status": QUEUED,
        "lease_until": None,
        "attempts": 0,
        "created": _now(),
        "payload": payload or {},
        "result": None,
        "error": None,
    })
    return job_id


def find_open(db, job_type, asset_id):
    """A queued/running job of this type for this asset (e.g. left over from a
    caller that timed out), so retries reuse it instead of queueing a duplicate."""
    return db[COLLECTION].find_one(
        {"type": job_type, "asset_id": asset_id, "status": {"$in": [QUEUED, RUNNING]}})


def get(db, job_id):
    return db[COLLECTION].find_one({"_id": job_id})


def _check(job):
    if job is None:
        raise JobFailed("job disappeared")
    if job["status"] == DONE:
        return job.get("result") or {}
    if job["status"] == FAILED:
        raise JobFailed(job.get("error") or "job failed")
    return None


def wait(db, job_id, timeout=120, poll=0.5):
    """Blocking wait (for code already running in a thread)."""
    deadline = time.monotonic() + timeout
    while True:
        out = _check(get(db, job_id))
        if out is not None:
            return out
        if time.monotonic() >= deadline:
            raise JobTimeout(f"job {job_id[:8]} not finished after {timeout}s")
        time.sleep(poll)


async def wait_async(db, job_id, timeout=120, poll=0.5):
    """Non-blocking wait for async handlers (bot / FastAPI)."""
    deadline = time.monotonic() + timeout
    while True:
        job = await asyncio.to_thread(get, db, job_id)
        out = _check(job)
        if out is not None:
            return out
        if time.monotonic() >= deadline:
            raise JobTimeout(f"job {job_id[:8]} not finished after {timeout}s")
        await asyncio.sleep(poll)


def run(db, job_type, payload=None, asset_id=None, timeout=120):
    """enqueue + wait; for sync code in threads."""
    return wait(db, enqueue(db, job_type, payload, asset_id), timeout=timeout)


async def run_async(db, job_type, payload=None, asset_id=None, timeout=120):
    job_id = await asyncio.to_thread(enqueue, db, job_type, payload, asset_id)
    return await wait_async(db, job_id, timeout=timeout)


# ---------------------------------------------------------------- worker side
def claim(db, types, lease_seconds=DEFAULT_LEASE_SECONDS):
    """Atomically take the oldest runnable job of one of `types`, or None.

    Runnable = queued, or running with an expired lease (worker crashed / was
    redeployed). attempts is incremented on every claim, so a job that keeps
    killing its worker cannot loop forever: once attempts is already at
    MAX_ATTEMPTS the claim is refused and `reap_exhausted` marks it failed.
    """
    now = _now()
    return db[COLLECTION].find_one_and_update(
        {
            "type": {"$in": list(types)},
            "attempts": {"$lt": MAX_ATTEMPTS},
            "$or": [
                {"status": QUEUED},
                {"status": RUNNING, "lease_until": {"$lt": now}},
            ],
        },
        {
            "$set": {"status": RUNNING, "lease_until": now + timedelta(seconds=lease_seconds),
                     "started_at": now},
            "$inc": {"attempts": 1},
        },
        sort=[("created", 1)],
        return_document=True,
    )


def extend_lease(db, job_id, lease_seconds=DEFAULT_LEASE_SECONDS):
    db[COLLECTION].update_one(
        {"_id": job_id, "status": RUNNING},
        {"$set": {"lease_until": _now() + timedelta(seconds=lease_seconds)}})


def complete(db, job_id, result=None):
    now = _now()
    db[COLLECTION].update_one(
        {"_id": job_id},
        {"$set": {"status": DONE, "result": result or {}, "error": None,
                  "lease_until": None, "finished_at": now}})


def fail(db, job, error):
    """Handler raised. attempts was already counted at claim time:
    below MAX_ATTEMPTS -> back to queued for a retry; otherwise -> failed."""
    now = _now()
    text = str(error)[:500]
    if job["attempts"] >= MAX_ATTEMPTS:
        db[COLLECTION].update_one(
            {"_id": job["_id"]},
            {"$set": {"status": FAILED, "error": text, "lease_until": None,
                      "finished_at": now}})
        return FAILED
    db[COLLECTION].update_one(
        {"_id": job["_id"]},
        {"$set": {"status": QUEUED, "error": text, "lease_until": None}})
    return QUEUED


def reap_exhausted(db, types):
    """Jobs whose worker died on the last allowed attempt never reach fail();
    mark them failed so waiters get an answer instead of a timeout."""
    now = _now()
    return db[COLLECTION].update_many(
        {"type": {"$in": list(types)}, "status": RUNNING,
         "attempts": {"$gte": MAX_ATTEMPTS}, "lease_until": {"$lt": now}},
        {"$set": {"status": FAILED, "error": "worker died on last attempt",
                  "lease_until": None, "finished_at": now}}).modified_count


# ---------------------------------------------------------------- scratch blobs
# Job inputs/outputs (edit results, rendered stories) live in their OWN GridFS
# bucket, separate from asset media, so they can be swept by age without any
# risk of touching a real asset. worker.py runs cleanup_blobs() periodically.
SCRATCH_BUCKET = "jobblobs"
SCRATCH_MAX_AGE_HOURS = 6


def _fs(db):
    import gridfs
    return gridfs.GridFS(db, collection=SCRATCH_BUCKET)


def put_blob(db, data: bytes, filename: str = "") -> str:
    return str(_fs(db).put(data, filename=filename))


def get_blob(db, blob_id: str) -> bytes:
    from bson import ObjectId
    return _fs(db).get(ObjectId(blob_id)).read()


def delete_blob(db, blob_id: str) -> None:
    from bson import ObjectId
    try:
        _fs(db).delete(ObjectId(blob_id))
    except Exception:
        logger.debug("scratch blob %s already gone", blob_id)


def cleanup_blobs(db, max_age_hours=SCRATCH_MAX_AGE_HOURS) -> int:
    """Delete scratch blobs older than max_age_hours (files AND chunks).

    A plain TTL index on `jobblobs.files` would drop only the file document and
    leave the chunks behind, so the space would never come back. Going through
    the GridFS API removes both.
    """
    fs = _fs(db)
    cutoff = _now() - timedelta(hours=max_age_hours)
    n = 0
    for f in db[f"{SCRATCH_BUCKET}.files"].find({"uploadDate": {"$lt": cutoff}}, {"_id": 1}):
        try:
            fs.delete(f["_id"])
            n += 1
        except Exception:
            logger.warning("could not delete scratch blob %s", f["_id"], exc_info=True)
    return n


# ---------------------------------------------------------------- worker heartbeat
# The web/bot service has no ffmpeg any more, so it cannot probe for it locally.
# Workers publish what they can do; the service asks "is a reel-capable worker alive?".
HEARTBEAT_COLLECTION = "media_workers"
HEARTBEAT_MAX_AGE = 120


def heartbeat(db, worker_id, types, caps=None):
    db[HEARTBEAT_COLLECTION].update_one(
        {"_id": worker_id},
        {"$set": {"types": list(types), "caps": caps or {}, "at": _now()}}, upsert=True)


def worker_available(db, job_type, cap=None, max_age=HEARTBEAT_MAX_AGE) -> bool:
    """True if a worker serving `job_type` (and having capability `cap`) beat recently."""
    q = {"types": job_type, "at": {"$gte": _now() - timedelta(seconds=max_age)}}
    if cap:
        q[f"caps.{cap}"] = True
    return db[HEARTBEAT_COLLECTION].find_one(q) is not None
