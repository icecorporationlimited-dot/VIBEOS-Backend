"""YouTube Data API v3 adapter (official) — Reels-relevant signals for India.

Two real signals, both from YouTube (a proxy for Instagram, NOT Instagram data):
  1. Music chart (category 10, mostPopular, IN)         -> kind "audio"   (song ideas)
  2. Top recent Shorts (<=60s, last 7 days, 3 categories) -> kind "hashtag" (hashtags that
     appear across several top Shorts)

Needs YOUTUBE_API_KEY. Without it nothing is stored — no fake data.
Quota: search.list = 100 units x 3 categories + a few videos.list calls (daily limit 10,000).
Audio rights are never marked verified.
"""
import logging
import re
from collections import Counter
from datetime import timedelta

import requests
from trends.schema import make_item, utcnow

log = logging.getLogger(__name__)
BASE = "https://www.googleapis.com/youtube/v3"
SHORT_CATEGORIES = ["10", "24", "26"]  # Music, Entertainment, Howto & Style
MAX_SHORT_SECONDS = 60
MIN_HASHTAG_VIDEOS = 2
SOURCE_NOTE = "YouTube signal (proxy); Instagram availability/licence of the audio NOT verified"

# Not Reels-material: promos, gaming, news, live streams ...
NOISE = re.compile(r"\b(trailer|teaser|promo|gameplay|gaming|live|news|full movie|episode|podcast|review)\b", re.I)
# Hashtags that carry no trend information
GENERIC_TAGS = {"shorts", "short", "viral", "trending", "fyp", "reels", "reel", "youtubeshorts",
                "ytshorts", "youtube", "subscribe", "foryou", "explore", "like", "share", "video",
                "india", "love", "new", "status", "whatsappstatus"}


def parse_duration(iso):
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 10**9
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


def song_name(title):
    """'Sammakka - Video Song | The Paradise | Nani' -> 'Sammakka'."""
    t = title.split("|")[0]
    t = re.split(r"\s[-–—]\s", t)[0]
    t = re.sub(r"\((official|lyric|video|audio)[^)]*\)|\[[^\]]*\]", "", t, flags=re.I)
    t = re.sub(r"#\w+", "", t)
    return re.sub(r"\s+", " ", t).strip(" -|")


def hashtags(text):
    return {h.lower() for h in re.findall(r"#([^\W_]{2,30})", text or "", flags=re.U)} - GENERIC_TAGS


def _get(http, path, params):
    r = http.get(f"{BASE}/{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def music_chart(api_key, region, http, now, limit=15):
    try:
        vids = _get(http, "videos", {"part": "snippet,statistics", "chart": "mostPopular",
                                     "regionCode": region, "videoCategoryId": "10",
                                     "maxResults": 25, "key": api_key}).get("items", [])
    except Exception as exc:
        log.warning("YouTube music chart failed: %s", type(exc).__name__)
        return []
    items, rank = [], 0
    for v in vids:
        title = v.get("snippet", {}).get("title", "")
        name = song_name(title)
        if not name or NOISE.search(title):
            continue
        rank += 1
        if rank > limit:
            break
        try:
            items.append(make_item(
                kind="audio", title=name, source_type="sourced",
                source_name=f"youtube_music_chart_{region}",
                source_url=f"https://www.youtube.com/watch?v={v['id']}",
                evidence={"rank": rank, "full_title": title,
                          "channel": v["snippet"].get("channelTitle"),
                          "views": int(v.get("statistics", {}).get("viewCount", 0))},
                ttl_hours=36, confidence=max(0.5, 0.85 - 0.02 * (rank - 1)),
                observed_at=now, rights_note=SOURCE_NOTE))
        except (ValueError, KeyError):
            continue
    return items


def shorts_hashtags(api_key, region, http, now):
    after = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids = []
    for cat in SHORT_CATEGORIES:
        try:
            res = _get(http, "search", {"part": "id", "type": "video", "videoDuration": "short",
                                        "order": "viewCount", "regionCode": region,
                                        "videoCategoryId": cat, "publishedAfter": after,
                                        "maxResults": 25, "key": api_key})
            ids += [i["id"]["videoId"] for i in res.get("items", []) if i.get("id", {}).get("videoId")]
        except Exception as exc:
            log.warning("YouTube shorts search (cat %s) failed: %s", cat, type(exc).__name__)
    ids = list(dict.fromkeys(ids))
    shorts = []
    for i in range(0, len(ids), 50):
        try:
            shorts += _get(http, "videos", {"part": "snippet,contentDetails,statistics",
                                            "id": ",".join(ids[i:i + 50]), "key": api_key}).get("items", [])
        except Exception as exc:
            log.warning("YouTube videos.list failed: %s", type(exc).__name__)
    shorts = [v for v in shorts
              if parse_duration(v.get("contentDetails", {}).get("duration")) <= MAX_SHORT_SECONDS
              and not NOISE.search(v.get("snippet", {}).get("title", ""))]
    if not shorts:
        return []
    counts, samples = Counter(), {}
    for v in shorts:
        sn = v.get("snippet", {})
        tags = hashtags(sn.get("title", "") + " " + sn.get("description", "")[:300])
        tags |= {t.lower().lstrip("#") for t in sn.get("tags", [])[:5]} - GENERIC_TAGS
        for t in tags:
            counts[t] += 1
            samples.setdefault(t, []).append(v["id"])
    items = []
    for tag, n in counts.most_common(15):
        if n < MIN_HASHTAG_VIDEOS:
            break
        try:
            items.append(make_item(
                kind="hashtag", title=f"#{tag}", source_type="sourced",
                source_name=f"youtube_shorts_{region}",
                source_url=f"https://www.youtube.com/watch?v={samples[tag][0]}",
                evidence={"videos": n, "of_shorts": len(shorts), "window_days": 7,
                          "sample_ids": samples[tag][:3]},
                ttl_hours=36, confidence=min(0.85, 0.5 + 0.07 * n),
                observed_at=now))
        except ValueError:
            continue
    return items


def fetch(api_key, region="IN", session=None):
    http = session or requests
    now = utcnow()
    return music_chart(api_key, region, http, now) + shorts_hashtags(api_key, region, http, now)
