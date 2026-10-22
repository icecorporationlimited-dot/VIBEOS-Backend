"""Step 4 — Context & emotion engine.

For one verified trend decide: what happened (context), which MOOD fits, and up to 3 ANGLES
(distinct reaction styles: hype / relatable / appreciation ...). Step 5 writes copy per angle.

Works with or without an LLM: without one (or if it fails / returns junk) the safe per-category
defaults from config.DEFAULT_ANGLES are used. Hard rules the LLM cannot override:
  * solemn / tragedy trends are ALWAYS mood=solemn, angle=tribute, one angle only
  * only the headline + publishers are given as facts; no invented details
`llm` is anything with .chat(system, user, temperature=..., max_tokens=...) -> str
(your engagement.openrouter_ai.OpenRouterAI already has this).
"""
import json
import logging
import re
from datetime import datetime, timezone

from . import config as cfg
from .db import ANGLES, MOODS, TRENDS
from .verify import publishers

log = logging.getLogger(__name__)

SYSTEM = (
    "You are the context analyst for a social-media story engine. Given ONE verified news/trend "
    "headline, return strict JSON only (no prose, no code fences) with keys: "
    '"what" (one plain sentence of what happened), "who" (list of people/teams named in the headline), '
    '"when" (time wording from the headline or ""), "mood" (one of: hype, funny, wholesome, reflective, solemn), '
    '"angles" (2-3 objects {"mood","angle"}; angle is one of: surprise, relatable, achievement, tribute, celebration; '
    "each angle must be a genuinely different reaction, not a rewording). "
    "Use ONLY information present in the headline. Never invent quotes, scores, causes, personal grief or "
    "details. If it is a death or tragedy use mood solemn and angle tribute only. Be conservative when unsure."
)


def _is_solemn(trend) -> bool:
    return trend.get("category") == "tragedy" or "solemn" in trend.get("flags", [])


def defaults(trend) -> dict:
    cat = "tragedy" if _is_solemn(trend) else trend.get("category", "news")
    plan = cfg.DEFAULT_ANGLES.get(cat, cfg.DEFAULT_ANGLES["news"])
    return {
        "context": {"what": trend.get("title", ""), "who": [], "when": ""},
        "mood": plan[0][0],
        "angles": [{"mood": m, "angle": a} for m, a in plan][: cfg.MAX_ANGLES],
        "source": "default",
    }


def _parse(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def _clip(v, n):
    return " ".join(str(v or "").split())[:n]


def validate(raw: dict, trend) -> dict | None:
    """Turn raw LLM JSON into a safe result, or None if unusable."""
    if not raw:
        return None
    base = defaults(trend)
    if _is_solemn(trend):                                   # hard rule: LLM cannot change tone
        out = dict(base)
        out["context"] = {"what": _clip(raw.get("what"), 200) or base["context"]["what"],
                          "who": [_clip(w, 60) for w in (raw.get("who") or [])[:5]],
                          "when": _clip(raw.get("when"), 60)}
        out["source"] = "llm"
        return out
    mood = raw.get("mood")
    angles, seen = [], set()
    for a in raw.get("angles") or []:
        if not isinstance(a, dict):
            continue
        m, g = a.get("mood"), a.get("angle")
        if m in MOODS and g in ANGLES and m != "solemn" and (m, g) not in seen:
            seen.add((m, g))
            angles.append({"mood": m, "angle": g})
    if len(angles) < 2:                                     # too thin: top up from defaults
        for m, g in cfg.DEFAULT_ANGLES.get(trend.get("category"), []):
            if (m, g) not in seen and len(angles) < 2:
                seen.add((m, g)); angles.append({"mood": m, "angle": g})
    if not angles:
        return None
    return {
        "context": {"what": _clip(raw.get("what"), 200) or base["context"]["what"],
                    "who": [_clip(w, 60) for w in (raw.get("who") or [])[:5] if w],
                    "when": _clip(raw.get("when"), 60)},
        "mood": mood if mood in MOODS and mood != "solemn" else angles[0]["mood"],
        "angles": angles[: cfg.MAX_ANGLES],
        "source": "llm",
    }


def analyze(trend, llm=None) -> dict:
    if llm is not None:
        user = (f"Category: {trend.get('category')}\nHeadline: {trend.get('title')}\n"
                f"Publishers: {', '.join(publishers(trend)[:5])}")
        try:
            raw = _parse(llm.chat(SYSTEM, user, temperature=0.3, max_tokens=350))
            res = validate(raw, trend)
            if res:
                return res
        except Exception as exc:
            log.warning("context LLM failed: %s", type(exc).__name__)
    return defaults(trend)


def process(db, llm=None, limit=50) -> int:
    """Analyze verified trends that have no context yet. Returns how many were done."""
    n = 0
    rows = db[TRENDS].find({"verification_status": "verified", "context": {"$exists": False}}).limit(limit)
    for t in rows:
        res = analyze(t, llm)
        db[TRENDS].update_one({"trend_id": t["trend_id"]}, {"$set": {
            "context": res["context"], "mood": res["mood"], "angles": res["angles"],
            "analysis_source": res["source"], "analyzed_at": datetime.now(timezone.utc)}})
        n += 1
    return n
