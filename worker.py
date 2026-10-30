"""worker.py — Render Background Worker for heavy media jobs.

    python worker.py                 # loop: claim -> run in a subprocess -> done/failed
    python worker.py --run <job_id>  # (internal) execute one job in this process

Env:
    MONGODB_URI   required (read by settings/memory.py)
    JOB_TYPES     comma list this worker serves, e.g. "prepare_ready,edit"
                  or "reel,smart_story,story_card"; empty/unset = all types.

Why a subprocess per job: Pillow/numpy/ffmpeg memory is returned to the OS when
the child exits, a hung ffmpeg can be killed, and a crash cannot take the claim
loop down. Lost jobs (worker killed mid-job) are reclaimed when their lease
expires; after 3 attempts a job is marked failed.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import jobs

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger("vibeos.worker")

IDLE_SLEEP = 1.0                      # seconds between polls when the queue is empty
CLEANUP_EVERY = 900                   # seconds between blob / rejected-asset sweeps
REJECTED_BLOB_DAYS = 7
# Per-type hard limit for the child process; must be < jobs.DEFAULT_LEASE_SECONDS.
TIMEOUTS = {"reel": 340, "prepare_ready": 120, "edit": 120, "smart_story": 90, "story_card": 60, "collage_story": 200, "business_content": 120, "business_render": 90}

_stop = False


def job_types():
    raw = os.environ.get("JOB_TYPES", "")
    types = [t.strip() for t in raw.split(",") if t.strip()]
    unknown = [t for t in types if t not in jobs.TYPES]
    if unknown:
        raise SystemExit(f"JOB_TYPES has unknown types {unknown}; valid: {list(jobs.TYPES)}")
    return types or list(jobs.TYPES)


def _db():
    import memory                      # pymongo only; the same connection settings as the bot
    return memory.db


# ------------------------------------------------------------------ child
def run_one(job_id: str) -> int:
    """Executed in the child process: heavy imports happen here, not in the loop."""
    import job_handlers
    db = _db()
    job = jobs.get(db, job_id)
    if job is None:
        print(f"job {job_id} not found", file=sys.stderr)
        return 2
    try:
        result = job_handlers.HANDLERS[job["type"]](job, db)
    except Exception as exc:           # parent turns this into attempts/retry/failed
        logging.getLogger("vibeos.worker").exception("job %s (%s) failed", job_id[:8], job["type"])
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    jobs.complete(db, job_id, result)
    return 0


# ------------------------------------------------------------------ parent
def _run_in_subprocess(job) -> tuple[bool, str]:
    timeout = TIMEOUTS.get(job["type"], 120)
    cmd = [sys.executable, str(BASE_DIR / "worker.py"), "--run", job["_id"]]
    try:
        proc = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _relay(exc.stderr, job)                 # shows how far the child got before it was killed
        return False, f"timeout after {timeout}s"
    _relay(proc.stderr, job)
    if proc.returncode == 0:
        return True, ""
    tail = (proc.stderr or "").strip().splitlines()[-1:] or [f"exit code {proc.returncode}"]
    return False, tail[0]


def _relay(stderr, job):
    """Child output is captured, so forward its log lines to the worker log (otherwise they are lost)."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    lines = [l for l in (stderr or "").splitlines() if l.strip()][-40:]
    for line in lines:
        logger.info("[%s] %s", job["_id"][:8], line)


def _sweep(db, types):
    n = jobs.cleanup_blobs(db)
    purged = purge_rejected_blobs(db) if "prepare_ready" in types else 0   # one worker owns this
    if n or purged:
        logger.info("cleanup: %s scratch blobs, %s rejected-asset blobs removed", n, purged)


def purge_rejected_blobs(db, days=REJECTED_BLOB_DAYS) -> int:
    """Free GridFS space held by assets rejected > `days` ago (never ready/posted media)."""
    from datetime import datetime, timedelta, timezone
    from content_buffer import GridFSBlobStore
    store = GridFSBlobStore(db)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    n = 0
    q = {"status": "rejected", "updated_at": {"$lt": cutoff}, "blob_id": {"$ne": None}}
    for a in db["assets"].find(q):
        ids = {a["blob_id"], *(((a.get("creative") or {}).get("variants") or {}).values())}
        for blob_id in filter(None, ids):
            try:
                store.delete(str(blob_id))
                n += 1
            except Exception:
                pass                                   # already gone
        db["assets"].update_one({"_id": a["_id"]}, {"$set": {"blob_id": None, "blobs_purged": True}})
    return n


def _heartbeat_loop(db, types, stop_event):
    import shutil
    import socket
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    caps = {"ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))}
    while not stop_event.is_set():
        try:
            jobs.heartbeat(db, worker_id, types, caps)
        except Exception:
            logger.warning("heartbeat failed", exc_info=True)
        stop_event.wait(30)
    try:
        db[jobs.HEARTBEAT_COLLECTION].delete_one({"_id": worker_id})
    except Exception:
        pass


def check_names():
    """Fail fast if media_names.py (used by the web service) drifted from editor.py."""
    import editor
    import media_names
    if tuple(editor.STYLE_NAMES) != media_names.STYLES or tuple(editor.SIZES) != media_names.SIZES:
        raise SystemExit("media_names.py is out of sync with editor.STYLE_NAMES/SIZES — update it.")


def _start_health_server():
    """Optional: when PORT is set (Render *web service* instead of a Background
    Worker), answer GET/HEAD with 200 so Render sees an open port and an external
    pinger can keep a free instance awake. No PORT -> does nothing."""
    port = os.environ.get("PORT")
    if not port:
        return
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Health(BaseHTTPRequestHandler):
        def _ok(self, body=True):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            if body:
                self.wfile.write(b"ok")

        def do_GET(self):
            self._ok()

        def do_HEAD(self):
            self._ok(body=False)

        def log_message(self, *_a):          # keep logs quiet
            pass

    server = ThreadingHTTPServer(("0.0.0.0", int(port)), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("health server listening on :%s", port)


def main() -> int:
    global _stop
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    types = job_types()
    _start_health_server()      # no-op unless PORT is set (web-service mode)
    db = _db()
    jobs.ensure_indexes(db)
    if "edit" in types or "prepare_ready" in types:
        check_names()
    import threading
    hb_stop = threading.Event()
    threading.Thread(target=_heartbeat_loop, args=(db, types, hb_stop), daemon=True).start()
    logger.info("worker up; serving %s", types)

    def _graceful(_s, _f):
        global _stop
        _stop = True
    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)

    last_sweep = 0.0
    while not _stop:
        try:
            jobs.reap_exhausted(db, types)
            if time.monotonic() - last_sweep > CLEANUP_EVERY:     # also under constant load
                last_sweep = time.monotonic()
                _sweep(db, types)
            job = jobs.claim(db, types)
            if job is None:
                time.sleep(IDLE_SLEEP)
                continue
            logger.info("job %s %s attempt %s", job["_id"][:8], job["type"], job["attempts"])
            ok, err = _run_in_subprocess(job)
            if not ok:
                fresh = jobs.get(db, job["_id"]) or job
                if fresh["status"] == jobs.RUNNING:        # child did not finish it itself
                    state = jobs.fail(db, fresh, err)
                    logger.warning("job %s -> %s (%s)", job["_id"][:8], state, err)
        except Exception:
            logger.exception("worker loop error; retrying in 5 s")
            time.sleep(5)
    hb_stop.set()
    logger.info("worker stopped")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--run":
        logging.basicConfig(level=logging.INFO)
        raise SystemExit(run_one(sys.argv[2]))
    raise SystemExit(main())
