"""story_engine/run_loop.py — Step 10a: pipeline ko background worker ki tarah chalao.

Render par Background Worker banao:  Start command:  python -m story_engine.run_loop
(Playwright wali Docker image chahiye, jaise render.story.yaml). Env: PIPELINE_EVERY_MIN=20
Ek Mongo lock hai, to galti se 2 instance chal gaye tab bhi pipeline overlap nahi karegi.
"""
import logging, os, signal, time
from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

from memory import db
from story_engine import db as se, rank, context, writer, visuals, layouts, quality, render, mongo_store
from story_engine.discovery import run

log = logging.getLogger("story_engine.loop")
LOCKS = "se_locks"
EVERY = int(os.environ.get("PIPELINE_EVERY_MIN", "20")) * 60
LOCK_TTL = 15 * 60          # crash ho jaye to lock 15 min baad khud chhoot jata hai
_stop = False


def _now():
    return datetime.now(timezone.utc)


def acquire(name="pipeline"):
    now = _now()
    try:
        db[LOCKS].update_one(
            {"_id": name, "$or": [{"expires_at": {"$lt": now}}, {"expires_at": {"$exists": False}}]},
            {"$set": {"expires_at": now + timedelta(seconds=LOCK_TTL), "owner": os.getpid()}},
            upsert=True)
        return True
    except DuplicateKeyError:               # lock kisi aur ke paas hai
        return False


def release(name="pipeline"):
    db[LOCKS].update_one({"_id": name}, {"$set": {"expires_at": _now() - timedelta(seconds=1)}})


def build_llm():
    from engagement.openrouter_ai import OpenRouterAI
    import settings
    return OpenRouterAI(getattr(settings, "OPENROUTER_API_KEY", ""),
                        getattr(settings, "OPENROUTER_MODEL", "openai/gpt-4o-mini"))


def run_once(llm):
    """Ek poora pass. Har step apne aap me guard hai: ek fail ho to baaki chalte hain."""
    steps = [
        ("discovery", lambda: run.save(db, run.discover_all())),
        ("rank",      lambda: rank.process(db)),
        ("context",   lambda: context.process(db, llm)),
        ("writer",    lambda: writer.process(db, llm)),
        ("visuals",   lambda: visuals.process(db)),
        ("layouts",   lambda: layouts.process(db)),
    ]
    for name, fn in steps:
        try:
            log.info("%s -> %s", name, fn())
        except Exception:
            log.exception("step %s failed; continuing", name)
    try:
        with render.PlaywrightRenderer() as r:
            log.info("render -> %s", render.process(db, r, mongo_store.MongoUploader(db)))
    except Exception:
        log.exception("render failed")
    try:
        log.info("quality -> %s", quality.process(db, llm=llm))   # sirf yahin se quality_status="ready"
    except Exception:
        log.exception("quality failed")
    try:
        log.info("purge -> %s old assets removed", mongo_store.purge(db, hours=48))
    except Exception:
        log.exception("purge failed")


def main():
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))
    se.ensure_indexes(db)
    layouts.sync(db)
    llm = build_llm()
    while not _stop:
        started = time.time()
        if acquire():
            try:
                run_once(llm)
            finally:
                release()
        else:
            log.info("another pipeline run is active; skipping")
        slept = 0
        while not _stop and slept < max(30, EVERY - (time.time() - started)):
            time.sleep(5); slept += 5


if __name__ == "__main__":
    main()
