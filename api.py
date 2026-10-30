"""api.py — web API for the VibeOS web app (Netlify) + reverse proxy to the bot's own server.

Runs in the same Render service as the Telegram bot (see main.py):

  public PORT  ->  this FastAPI app
      /api/...     web app endpoints (need X-API-Key header)
      everything   forwarded untouched to the bot's built-in server
      else         (/healthz, /oauth/callback, /media/..., /login, /privacy-policy)

So existing Instagram login / media / health-check URLs keep working.

Env vars:
  WEB_API_KEY       secret the web app sends in X-API-Key (API is disabled if unset)
  WEB_ORIGINS       comma-separated allowed origins, e.g. https://vibeos.netlify.app
  INTERNAL_PORT     port of the bot's internal server (set by main.py)
"""
import asyncio
import base64
import hmac
import io
import json
import logging
import os
import re

import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import copywriter
import settings
import jobs
from media_names import SIZES, STYLES   # names only; editing happens in worker.py

logger = logging.getLogger("vibeos.api")

WEB_API_KEY = os.environ.get("WEB_API_KEY", "")
ORIGINS = [o.strip().rstrip("/") for o in os.environ.get("WEB_ORIGINS", "").split(",") if o.strip()]
INTERNAL_PORT = int(os.environ.get("INTERNAL_PORT", "10001"))
MAX_UPLOAD = 12 * 1024 * 1024  # 12 MB
EDIT_WAIT_SECONDS = 45          # /api/edit waits this long, then answers 202 + job_id
TEXT_STYLES = ["minimal", "bold", "serif"]
TEXT_POSITIONS = ["bottom", "center", "top"]

app = FastAPI(title="VibeOS API", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,          # empty list = no browser origin allowed
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type"],
    expose_headers=["X-Edit-Report"],
)

_groq = None


def _groq_client():
    global _groq
    if _groq is None:
        from groq import Groq
        _groq = Groq(api_key=settings.GROQ_API_KEY)
    return _groq


def _db():
    import memory            # shared pymongo handle (no image libraries)
    return memory.db


def _jpeg_response(data: bytes, report: dict) -> Response:
    short = json.dumps({k: str(v)[:60] for k, v in (report or {}).items()})
    return Response(data, media_type="image/jpeg",
                    headers={"X-Edit-Report": short, "Cache-Control": "no-store"})


def _check_key(key: str | None):
    if not WEB_API_KEY:
        raise HTTPException(503, "Web API is not configured (WEB_API_KEY missing).")
    if not key or not hmac.compare_digest(key.encode(), WEB_API_KEY.encode()):
        raise HTTPException(401, "Invalid API key.")


async def _read_image(file: UploadFile) -> bytes:
    data = await file.read(MAX_UPLOAD + 1)
    if not data:
        raise HTTPException(400, "Empty file.")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Image too large (max 12 MB).")
    return data


def _parse_ai_json(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("No JSON object found in AI response")
    return json.loads(text[start:end + 1])


def _clean_overlay(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip().strip("\"'")
    text = "".join(ch for ch in text if ord(ch) < 0x2070)
    return " ".join(text.split()[:7])[:60]


# ----------------------------------------------------------------- endpoints
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/options")
def options(x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)
    return {
        "styles": ["automatic"],
        "editing_mode": "automatic",
        "sizes": ["automatic"],
        "text_styles": TEXT_STYLES,
        "text_positions": TEXT_POSITIONS,
        "caption_styles": list(copywriter.CAPTION_STYLES),
        "languages": list(copywriter.LANGUAGES),
    }


@app.post("/api/edit")
async def api_edit(
    file: UploadFile = File(...),
    style: str = Form("natural"),
    size: str = Form("post"),
    text: str = Form(""),
    text_style: str = Form("minimal"),
    text_position: str = Form("bottom"),
    x_api_key: str | None = Header(default=None),
):
    _check_key(x_api_key)
    # Style and output framing are intentionally not user-selectable.
    if text_style not in TEXT_STYLES or text_position not in TEXT_POSITIONS:
        raise HTTPException(400, "invalid text_style or text_position")
    data = await _read_image(file)
    db = _db()
    in_blob = await asyncio.to_thread(jobs.put_blob, db, data, "api_edit.jpg")
    job_id = await asyncio.to_thread(jobs.enqueue, db, "edit", {
        "in_blob": in_blob, "style": "natural", "auto_style": True,
        "size": "auto", "text": text.strip()[:80] or None,
        "text_required": bool(text.strip()),       # text only when explicitly provided
        "text_style": text_style, "text_position": text_position,
    })
    try:
        res = await jobs.wait_async(db, job_id, timeout=EDIT_WAIT_SECONDS)
    except jobs.JobTimeout:
        # Worker is busy or cold-starting: tell the client to poll /api/jobs/{id}.
        return JSONResponse({"job_id": job_id, "status": "queued"}, status_code=202)
    except jobs.JobFailed:
        logger.exception("edit failed")
        raise HTTPException(422, "Could not process this image.")
    out = await asyncio.to_thread(jobs.get_blob, db, res["blob_id"])
    await asyncio.to_thread(jobs.delete_blob, db, res["blob_id"])
    return _jpeg_response(out, res.get("report"))


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: str, x_api_key: str | None = Header(default=None)):
    """Poll an /api/edit job that answered 202: 202 while pending, 200 + JPEG when done."""
    _check_key(x_api_key)
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(404, "Not found")
    db = _db()
    job = await asyncio.to_thread(jobs.get, db, job_id)
    if job is None or job["type"] != "edit":
        raise HTTPException(404, "Not found")
    if job["status"] == jobs.FAILED:
        raise HTTPException(422, "Could not process this image.")
    if job["status"] != jobs.DONE:
        return JSONResponse({"job_id": job_id, "status": job["status"]}, status_code=202)
    res = job.get("result") or {}
    try:
        out = await asyncio.to_thread(jobs.get_blob, db, res["blob_id"])
    except Exception:
        raise HTTPException(410, "Result expired; submit the image again.")
    return _jpeg_response(out, res.get("report"))


@app.post("/api/caption")
async def api_caption(
    file: UploadFile = File(...),
    vibe: str = Form("natural"),
    mode: str = Form("natural"),
    language: str = Form("english"),
    x_api_key: str | None = Header(default=None),
):
    _check_key(x_api_key)
    if mode not in copywriter.CAPTION_STYLES:
        raise HTTPException(400, f"mode must be one of {list(copywriter.CAPTION_STYLES)}")
    if vibe not in STYLES:
        vibe = "natural"
    if language not in copywriter.LANGUAGES:
        language = "english"
    data = await _read_image(file)

    prompt = copywriter.build_prompt(mode=mode, vibe=vibe, language=language)
    image_url = "data:image/jpeg;base64," + base64.b64encode(data).decode()
    last_error = None
    for attempt in range(2):
        try:
            resp = await asyncio.to_thread(
                _groq_client().chat.completions.create,
                model=settings.VISION_MODEL,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ]}],
                temperature=0.9 if attempt == 0 else 0.6,
            )
            raw = _parse_ai_json(resp.choices[0].message.content)
            parsed = copywriter.parse_copy(raw, mode=mode)
            parsed["overlay"] = _clean_overlay(raw.get("overlay", ""))
            return JSONResponse(parsed)
        except Exception as exc:
            last_error = exc
            logger.warning("caption attempt %s failed: %s", attempt + 1, exc)
    logger.error("caption failed: %s", last_error)
    raise HTTPException(502, "AI caption failed. Try again.")


# Story library feed (story_engine step 10b): /api/story-feed, /list, /mark, /image/{id}.
# Must be included BEFORE the catch-all proxy route at the bottom of this file.
from story_engine.api_routes import make_router  # noqa: E402
app.include_router(make_router(_check_key, _db))


# Security headers for the hosted UI (the bot's own server already sets its own).
_UI_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self' data: blob:; connect-src 'self'; "
    "base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self' https://www.instagram.com https://api.instagram.com "
    "https://www.facebook.com https://m.facebook.com"
)


@app.middleware("http")
async def ui_security_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/app" or request.url.path.startswith("/app/"):
        response.headers["Content-Security-Policy"] = _UI_CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-cache"
    return response


# ------------------------------------------------- hosted web UI (same origin)
# Served from here so the login/CSRF cookies of /login and /web/* are first-party.
_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
if os.path.isdir(_WEB_DIR):
    app.mount("/app", StaticFiles(directory=_WEB_DIR, html=True), name="webui")


# ------------------------------------------------- proxy to the bot's server
_HOP = {"host", "connection", "keep-alive", "transfer-encoding", "te", "upgrade",
        "proxy-authorization", "proxy-authenticate", "trailers", "content-length",
        "content-encoding"}
_client = httpx.AsyncClient(
    base_url=f"http://127.0.0.1:{INTERNAL_PORT}", follow_redirects=False, timeout=90
)


@app.api_route("/{path:path}", methods=["GET", "POST", "HEAD", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    if path.startswith("api/"):
        raise HTTPException(404, "Not found")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    try:
        upstream = await _client.request(
            request.method, "/" + path, params=request.query_params,
            headers=headers, content=await request.body(),
        )
    except httpx.HTTPError:
        return Response("bot server unavailable", status_code=502, media_type="text/plain")
    resp = Response(upstream.content, status_code=upstream.status_code)
    for k, v in upstream.headers.multi_items():
        if k.lower() not in _HOP:
            resp.raw_headers.append((k.lower().encode(), v.encode()))
    return resp
