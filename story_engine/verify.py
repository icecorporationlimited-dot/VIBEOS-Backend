"""Step 3a — Verify & safety gate. Runs BEFORE scoring; virality never overrides it.

gate(trend) -> {"status": "verified" | "hold" | "rejected", "reasons": [...], "flags": [...]}
  rejected : topic type we never automate (see config.REJECT_WORDS)
  hold     : not enough proof YET (re-checked every run, may become verified when more sources appear)
  verified : safe to score and write
"""
import re
from datetime import datetime, timezone

from . import config as cfg
from . import safety


def _aware(dt):
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _has(text: str, words) -> list:
    t = text.lower()
    return [w for w in words if re.search(r"\b" + re.escape(w) + r"\b", t)]


def publishers(trend) -> list:
    seen, out = set(), []
    for s in trend.get("sources", []):
        n = (s.get("name") or "").strip()
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def age_hours(trend, now=None):
    now = now or datetime.now(timezone.utc)
    times = [_aware(s.get("published_at")) for s in trend.get("sources", []) if s.get("published_at")]
    if not times:
        return None
    return max(0.0, (now - max(times)).total_seconds() / 3600)   # newest source = how fresh the story is


def gate(trend, now=None) -> dict:
    title = trend.get("title", "")
    pubs = publishers(trend)
    heads = safety.source_headlines(trend)                  # each publisher's OWN headline (empty for old trends)
    reasons, flags = [], []
    is_calendar = trend.get("category") == "festival" and "event_calendar" in [p.lower() for p in pubs]

    # Sensitive topics: checked on the headline AND on every publisher's headline, stem/Hindi aware (safety.py).
    bad = safety.reject_hit(title, *[t for _, t in heads])
    if bad:
        return {"status": "rejected", "reasons": [f"sensitive topic not automated: {bad}"], "flags": ["sensitive"]}

    solemn = bool(_has(title, cfg.DEATH_WORDS))
    if solemn:
        flags.append("solemn")
    need = cfg.MIN_PUBLISHERS_SOLEMN if solemn else cfg.MIN_PUBLISHERS
    if not is_calendar and len(pubs) < need:
        reasons.append(f"only {len(pubs)} publisher(s), need {need}")

    # Trust gate: publisher COUNT is not enough; enough of them must be credible (config.CREDIBILITY).
    trusted = safety.trusted_publishers(pubs)
    need_trusted = safety.MIN_TRUSTED_SOLEMN if solemn else safety.MIN_TRUSTED
    if not is_calendar and len(trusted) < need_trusted:
        reasons.append(f"only {len(trusted)} trusted publisher(s), need {need_trusted}")

    rumour = _has(title, cfg.RUMOUR_WORDS)
    if rumour:
        flags.append("rumour")
        reasons.append(f"unconfirmed wording: {rumour[0]}")

    # Cross-check against what the publishers themselves wrote (disputes, numbers, deaths).
    if not is_calendar:
        more, more_flags = safety.crosscheck(title, heads, trusted, solemn)
        reasons += more
        flags += [f for f in more_flags if f not in flags]

        age = age_hours(trend, now)
        if age is None:
            reasons.append("no source timestamp")
        elif age > cfg.MAX_AGE_HOURS:
            return {"status": "rejected", "reasons": [f"stale ({age:.0f}h old)"], "flags": flags}

    if reasons:
        return {"status": "hold", "reasons": reasons, "flags": flags}
    return {"status": "verified", "reasons": [], "flags": flags}
