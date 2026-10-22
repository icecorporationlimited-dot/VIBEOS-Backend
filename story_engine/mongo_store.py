"""story_engine/mongo_store.py — rendered story images ko ALAG MongoDB (mongodb2) ke GridFS me rakhta hai.

Do databases:
  mongodb1 (main, `db` jo caller pass karta hai) : se_trends, se_story_assets, se_user_story_feed ... (metadata)
  mongodb2 (env IMAGES_MONGODB_URI)              : sirf GridFS (rendered JPEGs)

Env:
  IMAGES_MONGODB_URI   mongodb2 ka connection string. Khali ho to purane tareeke se main db me hi store hoga.
  IMAGES_DB_NAME       mongodb2 ke andar database ka naam (default: vibeos_story_images)

Pipeline me:   with render.PlaywrightRenderer() as r: render.process(db, r, MongoUploader(db))
Feed ke baad:  jpg = load_image(db, entry["rendered_image_url"])   # bytes, publishing queue ko do
Purane images: purge(db, hours=48)

rendered_image_url ab bhi "mongo://<file_id>" hota hai (file mongodb2 me, asset doc mongodb1 me).
Function signatures same hain, isliye run_loop.py / render.py / publishing code me koi change nahi.
"""
import io
import os
import threading
from datetime import datetime, timedelta, timezone

import gridfs
from bson import ObjectId
from pymongo import MongoClient

BUCKET = "se_story_fs"
PREFIX = "mongo://"

_lock = threading.Lock()
_client = None


def _images_db(main_db):
    """mongodb2 ka db (lazy, ek hi client). URI set nahi to main_db wapas (fallback)."""
    global _client
    uri = os.environ.get("IMAGES_MONGODB_URI", "").strip()
    if not uri:
        return main_db
    with _lock:
        if _client is None:
            _client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    return _client[os.environ.get("IMAGES_DB_NAME", "vibeos_story_images")]


def _fs(main_db):
    return gridfs.GridFS(_images_db(main_db), collection=BUCKET)


class MongoUploader:
    def __init__(self, db, quality=85):
        self._fs, self._q = _fs(db), quality

    def upload(self, key, data, content_type="image/png") -> str:
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=self._q, optimize=True)
        fid = self._fs.put(buf.getvalue(), filename=key.rsplit(".", 1)[0] + ".jpg",
                           content_type="image/jpeg", created_at=datetime.now(timezone.utc))
        return f"{PREFIX}{fid}"


def load_image(db, url) -> bytes:
    if not str(url).startswith(PREFIX):
        raise ValueError("not a mongo:// url")
    return _fs(db).get(ObjectId(url[len(PREFIX):])).read()


def purge(db, hours=48):
    """Purane assets mongodb1 se, unki image files mongodb2 se hatao."""
    from .db import ASSETS
    fs, cutoff, n = _fs(db), datetime.now(timezone.utc) - timedelta(hours=hours), 0
    for a in list(db[ASSETS].find({"created_at": {"$lt": cutoff}, "rendered_image_url": {"$ne": None}})):
        url = a["rendered_image_url"]
        if str(url).startswith(PREFIX):
            try:
                fs.delete(ObjectId(url[len(PREFIX):]))
            except Exception:
                pass
        db[ASSETS].delete_one({"_id": a["_id"]})
        n += 1
    return n
