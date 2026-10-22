"""Step 5 — Writer. One (trend, mood, angle) -> short natural copy that passes fact + duplicate checks.

Your 6 steps:  context -> mood -> angle (step 4)  |  natural copy -> fact-check -> duplicate check (here)
Needs an LLM (`llm.chat(system, user, temperature=, max_tokens=)`). Without one nothing is written:
a generic template would just be "headline + generic reaction", which the design rejects.
Facts come ONLY from the headline/context, so every check is strict and fail-closed.
"""
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from . import config as cfg
from .discovery.base import similar, tokens
from .db import ASSETS, TRENDS, new_asset

log = logging.getLogger(__name__)

ANGLE_HELP = {
    "celebration": "pure joy and party energy, short punchy lines",
    "relatable": "what a fan/regular person feels right now; shared emotion, one natural punchline",
    "surprise": "the unexpected twist or 'wait, what?' moment",
    "achievement": "appreciate the performance or the turning point that mattered",
    "tribute": "respectful, restrained remembrance; no jokes, no emoji, no drama",
}
MOOD_HELP = {
    "hype": "energetic, short", "funny": "playful, witty, never mean", "wholesome": "warm, kind",
    "reflective": "calm, thoughtful, neutral", "solemn": "respectful, restrained, simple words",
}
SYSTEM = (
    "You write the text of a single Instagram-story card in a natural human voice. "
    "Return strict JSON only: {\"lines\": [\"...\", \"...\"]}. Rules: vary sentence length and rhythm; do NOT restate "
    "the headline as a news headline; do NOT use generic filler ('what a day!'); no hashtags, no links; "
    "no quotation marks; use ONLY facts in the headline (no scores, numbers, names, causes you were not given); "
    "never invent quotes, personal experiences ('I was there', 'my friend'), or grief/closeness of any person. "
    "Never announce a death unless the headline itself says so."
)

# ---------------------------------------------------------------- fact check

_FIRST_PERSON = re.compile(
    r"\b(i|i'm|i've|mera|meri|mere|maine|mujhe)\b\s+(was|saw|watched|cried|felt|met|know|knew|dekha|tha|thi|raha|roya)\b"
    r"|\b(my|our|mere|humare)\s+(friend|dost|idol|hero|mentor|family|papa|mummy|bhai)\b", re.I)
_DEATH = re.compile(r"\b(rip|died|dies|death|passed away|no more|demise|gone too soon)\b", re.I)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
_QUOTE = re.compile(r"[\"“”‘’«»]")
_NUM = re.compile(r"\d[\d,.]*")


def fact_check(lines, trend, mood) -> list:
    """Return a list of problems; empty list = passed."""
    head = trend.get("title", "")
    who = " ".join((trend.get("context") or {}).get("who", []))
    known = tokens(head + " " + who) | cfg.NAME_ALLOWLIST
    text = " ".join(lines)
    solemn = mood == "solemn"
    bad = []
    if not lines:
        return ["empty copy"]
    lo, hi = cfg.LINES.get(mood, (1, 3))
    if len(lines) > hi + 1:
        bad.append(f"too many lines ({len(lines)} > {hi + 1})")
    if any(len(l) > cfg.MAX_LINE_CHARS for l in lines):
        bad.append(f"line over {cfg.MAX_LINE_CHARS} chars")
    if _QUOTE.search(text):
        bad.append("quotation marks (possible invented quote)")
    if "#" in text or "http" in text.lower():
        bad.append("hashtag/link")
    for n in _NUM.findall(text):
        if n.strip(".,") not in head:
            bad.append(f"number not in headline: {n}")
            break
    if _FIRST_PERSON.search(text):
        bad.append("personal experience/closeness claim")
    if _DEATH.search(text) and not (solemn and _DEATH.search(head)):
        bad.append("death wording not supported by headline")
    if solemn and _EMOJI.search(text):
        bad.append("emoji in solemn story")
    if similar(lines[0], head, 0.8):
        bad.append("first line just repeats the headline")
    for line in lines:                                       # invented names: mid-sentence Capitalised words
        for m in re.finditer(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z]{2,})\b", line):
            if m.group(1).lower() not in known:
                bad.append(f"name not in headline: {m.group(1)}")
                break
    return bad


JUDGE_SYSTEM = (
    "You are a strict fact-checker. Given a headline and a short story text, list every person, team, number, "
    "place, event or claim in the text that is NOT stated in the headline. Opinions and emotions are fine. "
    'Return strict JSON only: {"unsupported": ["..."]} (empty list if everything is supported).'
)


def judge(lines, trend, llm) -> list:
    """LLM fact-check pass (catches invented names/claims the regex rules cannot). Fail-closed on error."""
    user = f"Headline: {trend.get('title')}\nText: {' / '.join(lines)}"
    try:
        m = re.search(r"\{.*\}", llm.chat(JUDGE_SYSTEM, user, temperature=0, max_tokens=150) or "", flags=re.S)
        un = json.loads(m.group(0)).get("unsupported") if m else None
    except Exception:
        return ["fact-check judge failed"]
    if not isinstance(un, list):
        return ["fact-check judge returned junk"]
    return [f"unsupported claim: {str(u)[:40]}" for u in un[:3]]


# ----------------------------------------------------------- duplicate check

def copy_hash(lines) -> str:
    return hashlib.sha1(" ".join(sorted(tokens(" ".join(lines)))).encode("utf-8")).hexdigest()


def is_duplicate(lines, recent_copies) -> str | None:
    """recent_copies: list of line-lists from recent stories (any trend, any user)."""
    mine = " ".join(lines)
    h = copy_hash(lines)
    for other in recent_copies:
        if copy_hash(other) == h or similar(mine, " ".join(other), cfg.DUP_SIMILARITY):
            return "too similar to a recent story"
    return None


def recent_copies(db, now=None, limit=300):
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=cfg.DUP_LOOKBACK_DAYS)
    rows = db[ASSETS].find({"created_at": {"$gt": since}}, {"copy": 1}).limit(limit)
    return [r["copy"] for r in rows if r.get("copy")]


# -------------------------------------------------------------------- writing

def _prompt(trend, mood, angle, recent, feedback):
    lo, hi = cfg.LINES.get(mood, (2, 3))
    ctx = trend.get("context") or {}
    openers = [c[0] for c in recent[:6] if c]
    return (
        f"Headline (the only source of facts): {trend.get('title')}\n"
        f"What happened: {ctx.get('what') or trend.get('title')}\n"
        f"Mood: {mood} ({MOOD_HELP.get(mood, '')})\nAngle: {angle} ({ANGLE_HELP.get(angle, '')})\n"
        f"Language: {cfg.COPY_LANGUAGE}\n"
        f"Write {lo}-{hi} short lines (each under {cfg.MAX_LINE_CHARS} characters)"
        + (" - do not pad; fewer is fine." if mood == "solemn" else ".") + "\n"
        + (f"Do not start like these recent stories: {json.dumps(openers, ensure_ascii=False)}\n" if openers else "")
        + (f"Your last attempt was rejected because: {'; '.join(feedback)}. Fix that.\n" if feedback else "")
    )


def _parse_lines(text):
    m = re.search(r"\{.*\}", (text or ""), flags=re.S)
    if not m:
        return None
    try:
        lines = json.loads(m.group(0)).get("lines")
    except ValueError:
        return None
    if not isinstance(lines, list):
        return None
    return [" ".join(str(l).split()) for l in lines if str(l).strip()][:6]


def write_one(trend, mood, angle, llm, recent=()) -> dict:
    """Returns {"ok": True, "lines": [...]} or {"ok": False, "reasons": [...]}."""
    feedback = []
    for _ in range(cfg.WRITE_ATTEMPTS):
        try:
            lines = _parse_lines(llm.chat(SYSTEM, _prompt(trend, mood, angle, list(recent), feedback),
                                          temperature=0.9, max_tokens=300))
        except Exception as exc:
            return {"ok": False, "reasons": [f"llm error: {type(exc).__name__}"]}
        if not lines:
            feedback = ["output was not valid JSON with lines"]
            continue
        problems = fact_check(lines, trend, mood)
        if not problems:
            problems = judge(lines, trend, llm)          # second, LLM-based fact-check
        dup = is_duplicate(lines, recent)
        if dup:
            problems.append(dup)
        if not problems:
            return {"ok": True, "lines": lines}
        feedback = problems
    return {"ok": False, "reasons": feedback}


def process(db, llm, limit=20) -> dict:
    """Write copy for every analyzed trend angle that has none yet; save as pending assets."""
    stats = {"written": 0, "rejected": 0}
    if llm is None:
        return {**stats, "error": "no_llm"}
    recent = recent_copies(db)
    rows = db[TRENDS].find({"verification_status": "verified", "angles": {"$exists": True}}).limit(limit)
    for t in rows:
        for a in t["angles"]:
            if db[ASSETS].find_one({"trend_id": t["trend_id"], "mood": a["mood"], "angle": a["angle"]}):
                continue
            res = write_one(t, a["mood"], a["angle"], llm, recent)
            if not res["ok"]:
                stats["rejected"] += 1
                log.info("copy rejected %s/%s/%s: %s", t["trend_id"], a["mood"], a["angle"], res["reasons"])
                continue
            doc = new_asset(trend_id=t["trend_id"], mood=a["mood"], angle=a["angle"], copy=res["lines"],
                            image=None, layout_id=None, layout_version=None, copy_hash=copy_hash(res["lines"]))
            db[ASSETS].insert_one(doc)          # image/layout/render are filled by steps 6-8
            recent.insert(0, res["lines"])
            stats["written"] += 1
    return stats
