"""Generate a casual creator-style 9:16 Story draft from sourced trend metadata.

This renders a local image only and never publishes. The old news-card look
(TRENDING NOW / IN THE NEWS chips, search counts, SOURCE footer) is gone:
drafts now look like a person's minimal Story - varied layouts, lowercase casual
text, no source labels. The review-before-publish pipeline is unchanged.
"""
from __future__ import annotations

from pathlib import Path

from creative_studio.story_themes import render_story
from trends import story_ai


def render_trend_story(item: dict, output_path: str | Path, *, brand: str = "VIBEOS",
                       accent: tuple[int, int, int] = (126, 91, 255), font_path: str | None = None,
                       show_headline: bool = True) -> Path:
    """Render a casual Story draft for one trend term.

    Raises ValueError when the trend is not safe for a casual draft (tragedy
    terms, non-latin titles, unknown category) - the caller then skips it.
    """
    title = str(item.get("title", "")).strip()
    url = str(item.get("source_url", "")).strip()
    if not title or not url.startswith("https://"):
        raise ValueError("A title and HTTPS source URL are required for a trend Story draft")
    plan = story_ai.fallback_plan(item, variant=title)
    if plan is None:
        raise ValueError("trend is not safe for a casual story draft")
    return render_story(plan, output_path, photo_bytes=None, brand=brand, seed=title)
