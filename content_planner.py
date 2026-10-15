"""Balanced event-aware content planner for VibeOS.

Event assets are scheduled in their configured event window. General assets
continue filling the normal calendar slots, so a reserved event post never
means the account has to go quiet.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from event_calendar import find_event
from event_detector import classify_image
from publishing.policy import get_zone

logger = logging.getLogger(__name__)


class ContentPlanner:
    def __init__(self, buffer, calendar, *, api_key, model, tz_for,
                 event_calendar_path=None, min_confidence=0.78):
        self.buffer = buffer
        self.calendar = calendar
        self.api_key = api_key
        self.model = model
        self.tz_for = tz_for
        self.event_calendar_path = Path(event_calendar_path) if event_calendar_path else None
        self.min_confidence = float(min_confidence)

    def classify_asset(self, telegram_id, asset: dict) -> dict:
        """Classify once and cache the result in the asset document."""
        cached = asset.get("event_detection")
        if isinstance(cached, dict) and cached.get("checked_at"):
            return cached
        try:
            raw = self.buffer.get_media(asset["_id"])
            result = classify_image(raw, api_key=self.api_key, model=self.model)
        except Exception as exc:
            logger.warning("Event detection failed for asset %s: %s", asset.get("_id"), exc)
            result = {"event_related": False, "event_key": "", "content_type": "general",
                      "confidence": 0.0, "visual_summary": "", "suggested_caption_angle": "",
                      "error": str(exc)[:240]}
        result["checked_at"] = datetime.now(timezone.utc)
        self.buffer.assets.update_one(
            {"_id": asset["_id"], "telegram_id": str(telegram_id)},
            {"$set": {"event_detection": result}},
        )
        return result

    def schedule_for(self, telegram_id, asset: dict):
        """Return (UTC datetime or None, metadata). None means use normal slots."""
        detection = self.classify_asset(telegram_id, asset)
        if not detection.get("event_related") or detection.get("confidence", 0) < self.min_confidence:
            return None, {"event_status": "general_or_uncertain", "event_detection": detection}
        key = detection.get("event_key", "")
        event = find_event(key, self.event_calendar_path) if self.event_calendar_path else find_event(key)
        if not event:
            return None, {"event_status": "no_calendar_match", "event_detection": detection}

        tz = get_zone(self.tz_for(telegram_id))
        now_local = datetime.now(timezone.utc).astimezone(tz)
        start_day = event["date"] - timedelta(days=event["window_before_days"])
        end_day = event["date"] + timedelta(days=event["window_after_days"])
        if now_local.date() > end_day:
            return None, {"event_status": "event_window_passed", "event": event["key"],
                          "event_date": event["date"].isoformat(), "event_detection": detection}

        not_before = datetime.combine(max(start_day, now_local.date()), time(0, 0), tzinfo=tz)
        not_after = datetime.combine(end_day, time(23, 59), tzinfo=tz)
        when = self.calendar.next_slot(
            telegram_id, horizon_days=max(1, (end_day - now_local.date()).days + 1),
            not_before=not_before, not_after=not_after,
        )
        if when is None:
            return None, {"event_status": "window_full", "event": event["key"],
                          "event_date": event["date"].isoformat(), "event_detection": detection}
        return when, {"event_status": "reserved", "event": event["key"],
                      "event_name": event["name"], "event_date": event["date"].isoformat(),
                      "event_detection": detection}
