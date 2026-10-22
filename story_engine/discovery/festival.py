"""Festivals/occasions from your existing event_calendar.json (no network).

An event is a candidate while today is inside [date - window_before, date + window_after].
Calendar is official/known, so it counts as its own source.
"""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .base import candidate

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "event_calendar.json"


def discover(today: date | None = None, path=DEFAULT_PATH):
    today = today or datetime.now(timezone.utc).date()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for ev in data.get("events", []):
        d = date.fromisoformat(ev["date"])
        start = d - timedelta(days=ev.get("window_before_days", 0))
        end = d + timedelta(days=ev.get("window_after_days", 0))
        if start <= today <= end:
            when = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
            src = [{"name": "event_calendar", "url": f"calendar:{ev['key']}", "published_at": when}]
            c = candidate("festival", ev["name"], src, signal=ev.get("priority", 50), published_at=when)
            if c:
                c["calendar_key"] = ev["key"]
                out.append(c)
    return out
