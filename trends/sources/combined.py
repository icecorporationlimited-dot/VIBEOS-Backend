"""One fetch() for the Story pipeline: Google Trends + GNews + Reddit signal.

    fetch = make_fetch(region="IN", gnews_key=..., reddit_id=..., reddit_secret=...)
    items = fetch()          # same item shape as google_trends.fetch

Every extra source is optional; a missing key or a failing API just means fewer
signals, never an exception and never a looser safety check.
"""
from __future__ import annotations

from trends.sources import gnews, google_trends, reddit


def make_fetch(region: str = "IN", limit: int = 20, *, gnews_key: str = "",
               reddit_id: str = "", reddit_secret: str = "", lang: str = "en", session=None):
    def fetch() -> list[dict]:
        items = google_trends.fetch(region, session, limit)
        if not items:
            return []
        items = gnews.enrich(items, gnews_key, country=region, lang=lang, session=session)
        if reddit_id and reddit_secret:
            items = reddit.annotate(items, reddit.fetch(reddit_id, reddit_secret, session=session))
        return items
    return fetch
