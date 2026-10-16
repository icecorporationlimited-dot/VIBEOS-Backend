"""Licensed celebrity/public-figure photos from Wikimedia Commons.

We never scrape Google Images or news photos (copyright). Only files whose
Commons licence allows reuse are used, and the credit line is returned so it is
printed on the Story (CC BY / CC BY-SA require attribution).

Accepted: CC0, Public domain, CC BY, CC BY-SA. Rejected: NC / ND, fair use,
unknown or missing licence, tiny images, non-JPEG/PNG.
The person name must come from the verified headlines (see story_ai.sanitize_plan)
and the Wikipedia article title must match it, so a wrong person is never shown.
Photos of real people are never AI-generated.
"""
from __future__ import annotations

import html
import logging
import re

import requests

log = logging.getLogger(__name__)
WIKI = "https://en.wikipedia.org/w/api.php"
COMMONS = "https://commons.wikimedia.org/w/api.php"
UA = {"User-Agent": "VibeOS-StoryBot/1.0 (licensed-photo lookup)"}
OK_LICENSE = re.compile(r"^(cc0|public domain|pd\b|cc[ -]by(?![ -]?n[cd])(?:[ -]sa)?\b)", re.I)
BAD_LICENSE = re.compile(r"(\bnc\b|-nc|\bnd\b|-nd|fair use|non-?free|all rights reserved)", re.I)
MIN_SIDE = 500
MAX_BYTES = 6_000_000


def _strip(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def license_ok(short_name: str) -> bool:
    name = (short_name or "").strip()
    return bool(name and OK_LICENSE.match(name) and not BAD_LICENSE.search(name))


def _get(http, url, params):
    r = http.get(url, params=params, headers=UA, timeout=15)
    r.raise_for_status()
    return r


def _title_matches(name: str, title: str) -> bool:
    toks = [t for t in re.split(r"\W+", name.lower()) if t]
    low = title.lower()
    return bool(toks) and all(t in low for t in toks)


def find_person_photo(name: str, http=None):
    """Return {"bytes", "credit", "license", "source_url", "title"} or None. Never raises."""
    http = http or requests
    try:
        res = _get(http, WIKI, {"action": "query", "format": "json", "generator": "search",
                                "gsrsearch": name, "gsrlimit": 3, "prop": "pageimages|description",
                                "piprop": "name", "redirects": 1}).json()
        pages = sorted((res.get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
        page = next((p for p in pages if _title_matches(name, p.get("title", ""))
                     and "disambiguation" not in str(p.get("description", "")).lower()
                     and p.get("pageimage")), None)
        if not page:
            return None
        filename = "File:" + page["pageimage"]
        info = _get(http, COMMONS, {"action": "query", "format": "json", "titles": filename,
                                    "prop": "imageinfo", "iiprop": "url|extmetadata|size|mime",
                                    "iiurlwidth": 1080}).json()
        entry = next(iter((info.get("query") or {}).get("pages", {}).values()), {})
        ii = (entry.get("imageinfo") or [None])[0]       # missing on Commons => non-free local file
        if not ii or ii.get("mime") not in ("image/jpeg", "image/png"):
            return None
        meta = ii.get("extmetadata") or {}
        lic = _strip((meta.get("LicenseShortName") or {}).get("value"))
        if not license_ok(lic):
            return None
        if min(ii.get("width", 0), ii.get("height", 0)) < MIN_SIDE:
            return None
        img = _get(http, ii.get("thumburl") or ii["url"], {})
        data = img.content
        if not data or len(data) > MAX_BYTES:
            return None
        artist = _strip((meta.get("Artist") or {}).get("value"))[:40]
        credit = "Photo: " + (artist + " / " if artist else "") + lic + " / Wikimedia"
        return {"bytes": data, "credit": credit[:90], "license": lic,
                "source_url": ii.get("descriptionurl", ""), "title": page.get("title", "")}
    except Exception as exc:                              # network, JSON, anything -> no photo
        log.info("photo lookup failed (%s)", type(exc).__name__)
        return None
