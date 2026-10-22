"""Step 3b — Trend score.  S = 0.30V + 0.25C + 0.20R + 0.15F + 0.10D  (all terms 0-100).

V virality (discovery 'signal'), C source credibility, R audience relevance,
F freshness (exponential decay), D visual/story potential.
Only call this for trends the safety gate marked 'verified'.
"""
import re

from . import config as cfg
from .verify import age_hours, publishers


def virality(trend) -> float:
    return float(trend.get("signal") or 0)


def credibility(trend) -> float:
    ws = sorted((cfg.CREDIBILITY.get(p.lower(), cfg.DEFAULT_CREDIBILITY) for p in publishers(trend)), reverse=True)
    if not ws:
        return 0.0
    top = ws[:3]
    quality = sum(top) / len(top)
    breadth = min(100, len(ws) * 34)          # more independent publishers = more trust
    return 0.6 * quality + 0.4 * breadth


def relevance(trend) -> float:
    base = cfg.RELEVANCE.get(trend.get("category"), 50)
    words = set(re.findall(r"[a-z]+", trend.get("title", "").lower()))
    return min(100, base + (cfg.INDIA_BONUS if words & cfg.INDIA_WORDS else 0))


def freshness(trend, now=None) -> float:
    if trend.get("category") == "festival":
        return 100.0                           # inside its calendar window = fresh
    age = age_hours(trend, now)
    if age is None:
        return 0.0
    return 100 * 0.5 ** (age / cfg.FRESH_HALF_LIFE_HOURS)


def visual(trend) -> float:
    cat = "tragedy" if "solemn" in trend.get("flags", []) else trend.get("category")
    return float(cfg.VISUAL.get(cat, 50))


def score(trend, now=None) -> dict:
    parts = {"V": virality(trend), "C": credibility(trend), "R": relevance(trend),
             "F": freshness(trend, now), "D": visual(trend)}
    total = sum(cfg.WEIGHTS[k] * max(0, min(100, v)) for k, v in parts.items())
    return {"score": round(total, 2), "parts": {k: round(v, 1) for k, v in parts.items()}}
