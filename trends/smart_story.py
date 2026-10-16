"""Verify -> AI plan -> licensed photo -> themed render, for one trend item."""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from creative_studio.story_themes import render_story
from trends import photos as photos_mod
from trends import story_ai, verify

logger = logging.getLogger(__name__)


def build_story(item: dict, *, ai=None, brand="VIBEOS", blocked_topics=(), min_sources=2,
                min_traffic=10_000, allow_solemn=True, show_sources=True, photo_finder=None,
                user_photo: bytes | None = None, now=None, variant: str = "") -> dict:
    """Returns {"ok": bool, "reason", "steps", "plan", "jpg"}; jpg only when ok."""
    steps = verify.pre_check(item, blocked_topics=blocked_topics, min_sources=min_sources,
                             min_traffic=min_traffic, now=now)
    v = verify.verdict(steps)
    if not v["ok"]:
        return {**v, "plan": None, "jpg": None}

    plan = story_ai.plan_story(item, ai, variant=variant)
    if plan is None:
        return {"ok": False, "reason": "no_safe_plan", "steps": steps, "plan": None, "jpg": None}
    steps += verify.post_check(item, plan, allow_solemn=allow_solemn)
    v = verify.verdict(steps)
    if not v["ok"]:
        return {**v, "plan": plan, "jpg": None}

    photo, credit = user_photo, ""
    if photo is None and plan.get("person") and plan["kind"] != "game":
        found = (photo_finder or photos_mod.find_person_photo)(plan["person"])
        if found:
            photo, credit = found["bytes"], found["credit"]
    plan = {**plan, "photo_credit": credit}
    pubs = verify.publishers(item)
    footer = ("Source: " + ", ".join(pubs[:3])) if show_sources and pubs else ""
    try:
        with tempfile.TemporaryDirectory(prefix="vibeos_smart_") as tmp:
            path = Path(tmp) / "story.jpg"
            try:
                render_story(plan, path, photo_bytes=photo, brand=brand, footer=footer, seed=variant)
            except ValueError:
                if photo is None:
                    raise
                render_story({**plan, "photo_credit": ""}, path, photo_bytes=None, brand=brand, footer=footer, seed=variant)
            data = path.read_bytes()
    except (OSError, ValueError):
        logger.exception("Smart story render failed")
        return {"ok": False, "reason": "render_failed", "steps": steps, "plan": plan, "jpg": None}
    return {"ok": True, "reason": "ok", "steps": steps, "plan": plan, "jpg": data}
