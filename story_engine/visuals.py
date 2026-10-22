"""Step 6 — Visuals. Finds ONE licensed base image per trend (shared by all its angles) or a generic background.

Rule: no complete licence record -> the image is never used. Trend sources and image sources stay separate.
`http` is injectable (default `requests`) so tests run offline.
"""
import logging
import re
from datetime import datetime, timezone

from . import config as cfg
from .db import ASSETS, IMAGES, TRENDS
from .discovery.base import tokens

log = logging.getLogger(__name__)
REQUIRED = ("image_url", "source_url", "creator", "license", "attribution_required", "verified_at")


def _now():
    return datetime.now(timezone.utc)


def _strip(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def _get(http, url, **kw):
    if http is None:
        import requests as http
    r = http.get(url, timeout=12, **kw)
    r.raise_for_status()
    return r.json()


# ------------------------------------------------------------------ providers

def wikimedia(query, http=None):
    data = _get(http, "https://commons.wikimedia.org/w/api.php", headers={"User-Agent": cfg.HTTP_UA}, params={
        "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6, "gsrlimit": 8,
        "gsrsearch": f"{query} filetype:bitmap", "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": 1600})
    out = []
    for p in (data.get("query", {}).get("pages") or {}).values():
        ii = (p.get("imageinfo") or [{}])[0]
        m = ii.get("extmetadata") or {}
        if ii.get("mime") not in ("image/jpeg", "image/png"):
            continue
        out.append({
            "provider": "wikimedia", "image_url": ii.get("thumburl") or ii.get("url"),
            "source_url": ii.get("descriptionurl"), "creator": _strip(m.get("Artist", {}).get("value")),
            "license": (m.get("LicenseShortName") or {}).get("value", ""),
            "attribution_required": (m.get("AttributionRequired") or {}).get("value", "true").lower() != "false",
            "width": ii.get("thumbwidth") or ii.get("width") or 0, "height": ii.get("thumbheight") or ii.get("height") or 0,
            "text": f"{p.get('title', '')} {_strip((m.get('ImageDescription') or {}).get('value'))}",
        })
    return out


def unsplash(query, http=None):
    if not cfg.UNSPLASH_KEY:
        return []
    data = _get(http, "https://api.unsplash.com/search/photos", headers={"Authorization": f"Client-ID {cfg.UNSPLASH_KEY}"},
                params={"query": query, "orientation": "portrait", "per_page": 8})
    return [{"provider": "unsplash", "image_url": r["urls"]["regular"], "source_url": r["links"]["html"],
             "creator": r["user"]["name"], "license": "Unsplash License", "attribution_required": True,
             "width": r.get("width", 0), "height": r.get("height", 0),
             "text": f"{r.get('alt_description') or ''} {r.get('description') or ''}"} for r in data.get("results", [])]


def pexels(query, http=None):
    if not cfg.PEXELS_KEY:
        return []
    data = _get(http, "https://api.pexels.com/v1/search", headers={"Authorization": cfg.PEXELS_KEY},
                params={"query": query, "orientation": "portrait", "per_page": 8})
    return [{"provider": "pexels", "image_url": r["src"]["large2x"], "source_url": r["url"],
             "creator": r["photographer"], "license": "Pexels License", "attribution_required": False,
             "width": r.get("width", 0), "height": r.get("height", 0), "text": r.get("alt") or ""}
            for r in data.get("photos", [])]


PROVIDERS = {"wikimedia": wikimedia, "unsplash": unsplash, "pexels": pexels}

# ------------------------------------------------------------------- selection

def license_ok(rec) -> bool:
    lic = (rec.get("license") or "").lower().strip()
    if not lic or any(re.search(rf"\b{d}\b", lic) for d in cfg.LICENSE_DENY):
        return False
    return lic.startswith(cfg.LICENSE_ALLOW)


def usable(rec) -> bool:
    return (all(rec.get(k) not in (None, "") for k in REQUIRED if k not in ("attribution_required", "verified_at"))   # verified_at is stamped on pick
            and rec.get("attribution_required") is not None and license_ok(rec)
            and (rec.get("width") or 0) >= cfg.MIN_IMAGE_WIDTH)


def search_query(trend):
    ctx = trend.get("context") or {}
    words = list(ctx.get("who") or []) or sorted(tokens(trend.get("title", "")))[:4]
    return " ".join(words[:4])


def _score(rec, qtokens):
    overlap = len(qtokens & tokens(rec.get("text", ""))) / max(1, len(qtokens))      # rough relevance
    vertical = 1 if (rec.get("height") or 0) >= (rec.get("width") or 1) else 0.6
    return overlap * vertical


def pick_image(trend, http=None):
    """Best licensed image record for a trend, or None. Step 9 still re-checks relevance (photo_judge)."""
    if trend.get("category") in cfg.NO_PHOTO_CATEGORIES:
        return None
    q = search_query(trend)
    qt = tokens(q)
    best, best_s = None, 0.0
    for name in cfg.IMAGE_PROVIDERS:
        try:
            recs = PROVIDERS[name](q, http)
        except Exception:
            log.exception("image provider %s failed", name)       # one broken provider never stops the rest
            continue
        for r in recs:
            if not usable(r):
                continue
            s = _score(r, qt)
            if s > best_s:
                best, best_s = r, s
    return dict(best, verified_at=_now()) if best and best_s >= 0.34 else None


def generic_image(mood):
    return {"provider": "generic", "image_url": None, "background_css": cfg.GENERIC_BG.get(mood, cfg.GENERIC_BG["reflective"]),
            "source_url": None, "creator": "vibeos", "license": "own", "attribution_required": False,
            "verified_at": _now()}


def _public(rec):
    return {k: v for k, v in rec.items() if k not in ("text", "width", "height")} | \
           {"width": rec.get("width"), "height": rec.get("height")}


def process(db, http=None, limit=20) -> dict:
    stats = {"photo": 0, "generic": 0}
    todo = db[ASSETS].distinct("trend_id", {"image": None, "quality_status": "pending"})[:limit]
    for tid in todo:
        trend = db[TRENDS].find_one({"trend_id": tid})
        if not trend:
            continue
        rec = pick_image(trend, http)                      # ONE image per trend, shared by all its angles
        if rec:
            db[IMAGES].update_one({"image_url": rec["image_url"]}, {"$set": _public(rec)}, upsert=True)
            stats["photo"] += 1
        for a in db[ASSETS].find({"trend_id": tid, "image": None}):
            img = _public(rec) if rec and a["mood"] != "solemn" else generic_image(a["mood"])
            if not rec or a["mood"] == "solemn":
                stats["generic"] += 1
            db[ASSETS].update_one({"_id": a["_id"]}, {"$set": {"image": img}})
    return stats
