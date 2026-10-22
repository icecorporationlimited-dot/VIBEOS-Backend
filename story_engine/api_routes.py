"""story_engine/api_routes.py — Step 10b: user feed ke HTTP endpoints (api.py me include hote hain).

  GET  /api/story-feed?user_id=..&vibes=hype,funny   -> 1 nayi story (feed.pick_for_user) ya {"story": null}
  GET  /api/story-feed/list?user_id=..&status=unseen -> us user ki entries
  POST /api/story-feed/mark   {user_id, trend_id, status}
  GET  /api/story-feed/image/{story_asset_id}?user_id=..  -> JPEG (us user ka crop lagake)

Sab par X-API-Key chahiye (api.py ka _check_key). Image sirf usi user ko jiski entry hai.
Yahan koi research/render nahi hota: sirf library se uthana.
"""
import asyncio
import re

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from . import feed, mongo_store
from .variants import apply_crop

_USER = re.compile(r"[\w.@:\-]{1,64}")
_TREND = re.compile(r"[\w.:\-]{1,128}")
_ASSET = re.compile(r"[0-9A-Za-z]{1,64}")


class MarkBody(BaseModel):
    user_id: str
    trend_id: str
    status: str


def _user(value) -> str:
    if not _USER.fullmatch(value or ""):
        raise HTTPException(400, "Invalid user_id.")
    return value


def _public(entry: dict) -> dict:
    """Client ko sirf kaam ki cheezein; rendered_image_url (mongo://) andar ki baat hai, image endpoint do."""
    return {"story_asset_id": entry["story_asset_id"], "trend_id": entry["trend_id"],
            "title": entry.get("trend_title"), "mood": entry.get("mood"), "angle": entry.get("angle"),
            "copy": entry.get("copy"), "variant_id": entry["variant_id"], "status": entry["status"],
            "image_path": f"/api/story-feed/image/{entry['story_asset_id']}"}


def make_router(check_key, get_db) -> APIRouter:
    r = APIRouter()

    @r.get("/api/story-feed")
    async def next_story(user_id: str = Query(...), vibes: str = Query(""),
                         x_api_key: str | None = Header(default=None)):
        check_key(x_api_key)
        uid = _user(user_id)
        entry = await asyncio.to_thread(feed.pick_for_user, get_db(), uid, vibes.split(",") if vibes else None)
        return JSONResponse({"story": _public(entry) if entry else None})

    @r.get("/api/story-feed/list")
    async def my_stories(user_id: str = Query(...), status: str = Query("unseen"),
                         x_api_key: str | None = Header(default=None)):
        check_key(x_api_key)
        uid = _user(user_id)
        if status not in ("unseen", "seen", "posted", "skipped"):
            raise HTTPException(400, "Invalid status.")
        rows = await asyncio.to_thread(feed.list_feed, get_db(), uid, status)
        return JSONResponse({"stories": [_public(e) for e in rows]})

    @r.post("/api/story-feed/mark")
    async def mark(body: MarkBody, x_api_key: str | None = Header(default=None)):
        check_key(x_api_key)
        uid = _user(body.user_id)
        if not _TREND.fullmatch(body.trend_id):
            raise HTTPException(400, "Invalid trend_id.")
        try:
            ok = await asyncio.to_thread(feed.mark, get_db(), uid, body.trend_id, body.status)
        except ValueError:
            raise HTTPException(400, "status must be seen, posted or skipped.")
        if not ok:
            raise HTTPException(404, "Not found")
        return {"ok": True}

    @r.get("/api/story-feed/image/{story_asset_id}")
    async def story_image(story_asset_id: str, user_id: str = Query(...),
                          x_api_key: str | None = Header(default=None)):
        check_key(x_api_key)
        uid = _user(user_id)
        if not _ASSET.fullmatch(story_asset_id):
            raise HTTPException(404, "Not found")
        db = get_db()
        entry = await asyncio.to_thread(feed.get_entry, db, uid, story_asset_id)
        if not entry:
            raise HTTPException(404, "Not found")
        asset = await asyncio.to_thread(feed.asset_by_id, db, story_asset_id)
        if not asset or not asset.get("rendered_image_url"):
            raise HTTPException(404, "Not found")
        try:
            jpg = await asyncio.to_thread(mongo_store.load_image, db, asset["rendered_image_url"])
        except Exception:
            raise HTTPException(410, "Image expired.")
        jpg = await asyncio.to_thread(apply_crop, jpg, entry.get("crop"))
        return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    return r
