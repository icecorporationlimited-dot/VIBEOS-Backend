"""Read VibeOS's editable event calendar JSON."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

DEFAULT_PATH = Path(__file__).with_name("event_calendar.json")


def load_events(path=DEFAULT_PATH) -> list[dict]:
    """Load and validate events. Invalid entries are skipped safely."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    events = []
    for item in raw.get("events", []) if isinstance(raw, dict) else []:
        try:
            key = str(item["key"]).strip().lower().replace(" ", "_")
            event_date = date.fromisoformat(str(item["date"]))
            window_before = max(0, min(30, int(item.get("window_before_days", 1))))
            window_after = max(0, min(7, int(item.get("window_after_days", 0))))
            if key:
                events.append({
                    "key": key,
                    "name": str(item.get("name", key.replace("_", " ").title()))[:100],
                    "date": event_date,
                    "window_before_days": window_before,
                    "window_after_days": window_after,
                    "priority": max(1, min(100, int(item.get("priority", 80)))),
                })
        except (KeyError, TypeError, ValueError):
            continue
    return events


def find_event(event_key: str, path=DEFAULT_PATH):
    key = (event_key or "").strip().lower().replace(" ", "_")
    matches = [event for event in load_events(path) if event["key"] == key]
    return min(matches, key=lambda e: abs((e["date"] - date.today()).days)) if matches else None
