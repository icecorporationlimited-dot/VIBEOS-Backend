"""Build reviewable branded Story drafts from current sourced trend signals.

No direct publishing here by design: a search trend is not verified news.
Caller must fact-check the linked source and use the existing publishing layer
only after the account policy says the draft is eligible.
"""
from __future__ import annotations

from pathlib import Path
from trends.sources.google_trends import fetch as fetch_google_trends
from autonomous.decision_engine import decide_trend_action
from creative_studio.story_templates import render_trend_story


def create_story_drafts(output_dir: str | Path, *, region: str = "IN", limit: int = 5,
                        brand: str = "VIBEOS", brand_topics: list[str] | None = None,
                        blocked_terms: list[str] | None = None, session=None) -> list[dict]:
    """Fetch Google Trends and render eligible items as drafts (never publish)."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    items = fetch_google_trends(region=region, session=session, limit=max(limit, 1) * 3)
    drafts = []
    for index, item in enumerate(items):
        decision = decide_trend_action(item, brand_topics=brand_topics, blocked_terms=blocked_terms)
        if not decision.get("eligible"):
            continue
        slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in item["title"])[:48].strip("_") or f"trend_{index}"
        path = output_dir / f"trend_story_{slug}.jpg"
        try:
            render_trend_story(item, path, brand=brand)
        except (OSError, ValueError):
            continue
        drafts.append({"item": item, "decision": decision, "image_path": str(path),
                       "publish_status": "draft_only"})
        if len(drafts) >= limit:
            break
    return drafts
