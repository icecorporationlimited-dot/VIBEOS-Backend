"""Validated per-user occasion preferences (no heavy imports: used by autopilot settings)."""
from __future__ import annotations

import re

from .catalog import CATEGORIES, DEFAULT_CATEGORIES, TONES

DEFAULT_PREFS = {
    "enabled": True,
    "categories": list(DEFAULT_CATEGORIES),
    "auto_greeting": True,      # fill a near occasion with the best ready photo
    "min_priority": 70,
    "solemn": "respectful",     # respectful = calm tone | skip = never plan around solemn days
    "personal": [],
}


def clean_prefs(raw) -> dict:
    """Validate user-supplied occasion preferences (raises ValueError only)."""
    try:
        return _clean_prefs(raw)
    except (TypeError, AttributeError):
        raise ValueError("Invalid occasion settings.") from None


def _clean_prefs(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Occasion settings must be an object.")
    out = {}
    for k, v in raw.items():
        if k in ("enabled", "auto_greeting"):
            out[k] = bool(v)
        elif k == "categories":
            cats = [str(c).lower() for c in v]
            bad = [c for c in cats if c not in CATEGORIES]
            if bad:
                raise ValueError("Unknown occasion category: " + ", ".join(bad))
            out[k] = sorted(set(cats), key=CATEGORIES.index)
        elif k == "min_priority":
            n = int(v)
            if not 40 <= n <= 100:
                raise ValueError("Minimum priority must be 40-100.")
            out[k] = n
        elif k == "solemn":
            if v not in ("respectful", "skip"):
                raise ValueError("Solemn days: respectful or skip.")
            out[k] = v
        elif k == "personal":
            if not isinstance(v, list) or len(v) > 12:
                raise ValueError("At most 12 personal dates.")
            items = []
            for p in v:
                name = re.sub(r"\s+", " ", str(p.get("name", ""))).strip()[:40]
                month, day = int(p.get("month")), int(p.get("day"))
                if not name or not (1 <= month <= 12 and 1 <= day <= 31):
                    raise ValueError("Each personal date needs a name, month and day.")
                tone = p.get("tone") if p.get("tone") in TONES else "celebratory"
                items.append({"name": name, "month": month, "day": day, "tone": tone,
                              "lead": max(0, min(7, int(p.get("lead", 2)))),
                              "after": max(0, min(3, int(p.get("after", 1)))),
                              "priority": max(60, min(100, int(p.get("priority", 95))))})
            out[k] = items
        else:
            raise ValueError("Unknown occasion setting: " + str(k))
    return out
