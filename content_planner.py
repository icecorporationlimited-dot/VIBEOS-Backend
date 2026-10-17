"""Balanced event-aware content planner for VibeOS.

Event assets are scheduled in their configured event window. General assets
continue filling the normal calendar slots, so a reserved event post never
means the account has to go quiet.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from event_calendar import find_event
from event_detector import classify_image
from publishing.identity import Principal, owner_filter
from publishing.policy import get_zone, resolve_tz

logger = logging.getLogger(__name__)


def _principal(user):
    """Planner callers may pass a Principal (web or Telegram) or a plain Telegram ID."""
    return user if isinstance(user, Principal) else Principal.from_telegram(user)


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
        # Owner-scoped write: a cached detection can only land on this owner's asset.
        self.buffer.assets.update_one(
            {"$and": [{"_id": asset["_id"]}, owner_filter(_principal(telegram_id))]},
            {"$set": {"event_detection": result}},
        )
        return result

    def schedule_for(self, telegram_id, asset: dict):
        """Return (UTC datetime or None, metadata). None means use normal slots."""
        detection = self.classify_asset(telegram_id, asset)
        tz = get_zone(resolve_tz(self.tz_for, _principal(telegram_id)))
        now_local = datetime.now(timezone.utc).astimezone(tz)

        # A clearly printed date on a poster/invitation takes precedence over
        # generic slots and festival matching. For dates without a year, use
        # the next upcoming occurrence; never invent a date if parsing fails.
        raw_date = str(detection.get("visible_date_text") or "").strip()
        explicit_date, has_year = _parse_visible_date(raw_date, now_local.date())
        if explicit_date:
            if explicit_date >= now_local.date():
                not_before = datetime.combine(explicit_date, time(0, 0), tzinfo=tz)
                not_after = datetime.combine(explicit_date, time(23, 59), tzinfo=tz)
                when = self.calendar.next_slot(
                    telegram_id, horizon_days=1, not_before=not_before, not_after=not_after)
                if when is not None:
                    return when, {"event_status": "date_pinned", "event_date": explicit_date.isoformat(),
                                  "visible_date_text": raw_date, "event_detection": detection}
                return None, {"event_status": "date_full", "event_date": explicit_date.isoformat(),
                              "visible_date_text": raw_date, "event_detection": detection}
            return None, {"event_status": "printed_date_passed", "event_date": explicit_date.isoformat(),
                          "visible_date_text": raw_date, "event_detection": detection}

        if not detection.get("event_related") or detection.get("confidence", 0) < self.min_confidence:
            return None, {"event_status": "general_or_uncertain", "event_detection": detection}
        key = detection.get("event_key", "")
        event = find_event(key, self.event_calendar_path) if self.event_calendar_path else find_event(key)
        if not event:
            return None, {"event_status": "no_calendar_match", "event_detection": detection}

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


def _parse_visible_date(text: str, today: date):
    """Parse dates explicitly printed in content. Returns (date, has_year)."""
    if not text:
        return None, False
    import re
    from datetime import datetime as _dt

    cleaned = re.sub(r"[,]+", " ", text).strip()
    formats = (
        ("%Y-%m-%d", True), ("%d-%m-%Y", True), ("%d/%m/%Y", True),
        ("%m/%d/%Y", True), ("%d %B %Y", True), ("%B %d %Y", True),
        ("%d %b %Y", True), ("%b %d %Y", True),
        ("%d %B", False), ("%B %d", False), ("%d %b", False), ("%b %d", False),
    )
    for fmt, has_year in formats:
        try:
            parsed = _dt.strptime(cleaned, fmt).date()
            if not has_year:
                parsed = parsed.replace(year=today.year)
                if parsed < today:
                    parsed = parsed.replace(year=today.year + 1)
            return parsed, has_year
        except ValueError:
            continue
    # Numeric day/month without a year is ambiguous in some regions; don't guess.
    return None, False
