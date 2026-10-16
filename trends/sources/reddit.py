"""Reddit adapter (official OAuth API) - Gen-Z / meme / entertainment *signals*.

A Reddit post is a conversation signal, NOT a verified fact and NOT Instagram
data. Items carry no publishers, so trends.verify can never turn a Reddit item
into a Story on its own; they are used to annotate Google-Trends topics with how
much people are talking about them (evidence["reddit_hits"]) and as discovery input.

Needs REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET (a "script" or "web" app, app-only
client-credentials flow). Without them nothing is fetched. Check Reddit's current
API terms before using this in a commercial product: they restrict commercial use.
Response shape follows Reddit's documented listing format, not verified live here.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"
USER_AGENT = "VibeOS-TrendReader/1.0 (by u/vibeos-bot)"
DEFAULT_SUBREDDITS = ("IndianTeenagers", "bollywood", "indianpeoplefacebook", "cricket")


def _token(client_id, client_secret, http):
    r = http.post(TOKEN_URL, auth=(client_id, client_secret),
                  data={"grant_type": "client_credentials"},
                  headers={"User-Agent": USER_AGENT}, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


def fetch(client_id: str, client_secret: str, *, subreddits=DEFAULT_SUBREDDITS,
          per_subreddit: int = 25, min_score: int = 300, session=None, now=None) -> list[dict]:
    if not (client_id and client_secret):
        return []
    http = session or requests
    now = now or datetime.now(timezone.utc)
    try:
        token = _token(client_id, client_secret, http)
    except Exception as exc:
        log.warning("Reddit auth failed: %s", type(exc).__name__)
        return []
    headers = {"Authorization": f"bearer {token}", "User-Agent": USER_AGENT}
    out = []
    for sub in subreddits:
        if not re.fullmatch(r"[A-Za-z0-9_]{2,40}", str(sub)):
            continue
        try:
            r = http.get(f"{API}/r/{sub}/hot", params={"limit": max(1, min(50, per_subreddit))},
                         headers=headers, timeout=15)
            r.raise_for_status()
            children = r.json()["data"]["children"]
        except Exception as exc:
            log.warning("Reddit r/%s failed: %s", sub, type(exc).__name__)
            continue
        for ch in children:
            d = ch.get("data") or {}
            title = " ".join(str(d.get("title") or "").split())
            if (not title or d.get("stickied") or d.get("over_18")
                    or int(d.get("score") or 0) < min_score):
                continue
            out.append({
                "kind": "topic", "title": title[:250], "source_type": "sourced",
                "source_name": "reddit",
                "source_url": "https://www.reddit.com" + str(d.get("permalink") or ""),
                "evidence": {"subreddit": sub, "score": int(d.get("score") or 0),
                             "comments": int(d.get("num_comments") or 0)},
                "confidence": 0.4, "observed_at": now,
                "expires_at": now + timedelta(hours=6),
                "approx_traffic_numeric": None, "news": [], "needs_fact_check": True,
            })
    return out


def annotate(items: list[dict], reddit_items: list[dict]) -> list[dict]:
    """Add evidence["reddit_hits"]: Reddit titles sharing most key words with a trend.

    Informational only. verify.py does not use it, so it cannot approve anything.
    """
    from trends import verify
    if not reddit_items:
        return list(items)
    heads = [str(r.get("title", "")).lower() for r in reddit_items]
    out = []
    for it in items:
        toks = verify.key_tokens(str(it.get("title", "")))
        need = max(1, (len(toks) + 1) // 2)
        hits = sum(1 for h in heads if toks and sum(1 for t in toks if t in h) >= need)
        if hits:
            it = {**it, "evidence": {**(it.get("evidence") or {}), "reddit_hits": hits}}
        out.append(it)
    return out
