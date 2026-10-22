"""story_engine/web.py — run_loop ka web-service version.

Start command:  uvicorn story_engine.web:app --host 0.0.0.0 --port $PORT
Env: PIPELINE_SECRET=<koi lamba random string>, PIPELINE_EVERY_MIN=20 (optional)

GET  /healthz  -> Render health check (aur UptimeRobot ping ke liye)
POST /run      -> header X-Secret chahiye; pipeline ek baar background thread me chalata hai (turant 202)
Auto loop bhi chalta hai jab tak service jagi hai; lock ki wajah se overlap nahi hota.
"""
import hmac, logging, os, threading, time

from fastapi import FastAPI, Header, HTTPException

from story_engine import run_loop

log = logging.getLogger("story_engine.web")
app = FastAPI()
SECRET = os.environ.get("PIPELINE_SECRET", "")
_llm = None


def _pass():
    """Lock ke saath ek pass. Lock na mile to chup-chaap skip."""
    global _llm
    if not run_loop.acquire():
        return False
    try:
        _llm = _llm or run_loop.build_llm()
        run_loop.run_once(_llm)
        return True
    finally:
        run_loop.release()


def _loop():
    while True:
        try:
            _pass()
        except Exception:
            log.exception("pipeline pass failed")
        time.sleep(run_loop.EVERY)


@app.on_event("startup")
def _start():
    logging.basicConfig(level=logging.INFO)
    from memory import db
    from story_engine import db as se, layouts
    se.ensure_indexes(db)
    layouts.sync(db)
    threading.Thread(target=_loop, name="se-loop", daemon=True).start()


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/run")
def run_now(x_secret: str = Header(default="")):
    if not SECRET or not hmac.compare_digest(x_secret, SECRET):
        raise HTTPException(status_code=403)
    threading.Thread(target=_pass, daemon=True).start()
    return {"status": "started"}
