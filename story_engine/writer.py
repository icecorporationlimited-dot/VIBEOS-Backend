"""Step 5 — Writer. One (trend, mood, angle) -> short natural copy that passes fact + duplicate checks.

Your 6 steps:  context -> mood -> angle (step 4)  |  natural copy -> fact-check -> duplicate check (here)
Needs an LLM (`llm.chat(system, user, temperature=, max_tokens=)`). Without one nothing is written:
a generic template would just be "headline + generic reaction", which the design rejects.
Facts come ONLY from the headline/context, so every check is strict and fail-closed.
"""
import difflib
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from . import config as cfg
from .discovery.base import _norm, similar, tokens
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
    "the headline as a news headline; no hashtags, no links; "
    "NEVER use quotation marks of any kind (no ' or \" around words); "
    "every line must carry one concrete fact taken from the headline (who/what/result) - no filler lines "
    "like 'party mode on', 'full energy', 'what a day'; "
    "copy names EXACTLY as spelled in the headline and use no other names, teams, numbers, places or causes; "
    "never invent quotes, personal experiences ('I was there', 'my friend'), or grief/closeness of any person. "
    "Never announce a death unless the headline itself says so."
)

# --- cost controls (all overridable from env, no config.py change needed) ---
MAX_TRENDS_PER_RUN = int(os.getenv("WRITER_MAX_TRENDS", "5"))        # was limit=20
MAX_LLM_CALLS_PER_RUN = int(os.getenv("WRITER_MAX_LLM_CALLS", "30"))  # hard cap per run
USE_JUDGE = os.getenv("WRITER_USE_JUDGE", "1") != "0"                # set 0 to skip the LLM judge entirely
REJECTION_TTL_HOURS = int(os.getenv("WRITER_REJECTION_TTL_H", "72"))
REJECTIONS = "se_copy_rejections"
# Capitalised words that are never "invented names"
GENERIC_CAPS = {"team", "match", "game", "games", "fans", "fan", "cricket", "world", "cup", "final", "league",
                "series", "squad", "day", "night", "ab", "yeh", "yaar", "bhai", "dil", "ek", "hum", "sab", "aaj"}


class BudgetExceeded(RuntimeError):
    pass


class _CountingLLM:
    """Wraps llm so one run can never spend more than MAX_LLM_CALLS_PER_RUN chat calls."""

    def __init__(self, llm, cap):
        self.llm, self.cap, self.calls = llm, cap, 0

    def chat(self, *a, **kw):
        if self.calls >= self.cap:
            raise BudgetExceeded("llm call budget for this run used up")
        self.calls += 1
        return self.llm.chat(*a, **kw)


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
    known = _known_tokens(trend)
    text = " ".join(lines)
    solemn = mood == "solemn"
    bad = []
    if not lines:
        return ["empty copy"]
    lo, hi = cfg.LINES.get(mood, (1, 3))
    if len(lines) > hi + 1:
        bad.append(f"too many lines ({len(lines)} > {hi + 1})")
    cap = cfg.line_cap(mood)
    if any(len(l) > cap for l in lines):
        bad.append(f"line over {cap} chars (shorten every line to under {cap})")
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
    bad += [f"name not in headline: {w}" for _, w in _invented_names(lines, known)[:1]]
    return bad


def _known_tokens(trend):
    head = trend.get("title", "")
    who = " ".join((trend.get("context") or {}).get("who", []))
    return tokens(head + " " + who) | {_norm(w) for w in cfg.NAME_ALLOWLIST}


def _is_known(word, known) -> bool:
    """Normalised + fuzzy match: Games~game, Africa~African, Rajkumar~Rajkummar."""
    w = _norm(word.lower())
    if w in known or w in GENERIC_CAPS:
        return True
    return any(difflib.SequenceMatcher(None, w, k).ratio() >= 0.85 or (len(w) >= 4 and (k.startswith(w) or w.startswith(k)) and len(k) >= 4)
               for k in known)


def _invented_names(lines, known):
    """[(line_index, word)] for mid-sentence Capitalised words that are not in the headline."""
    out = []
    for i, line in enumerate(lines):
        for m in re.finditer(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z]{2,})\b", line):
            if not _is_known(m.group(1), known):
                out.append((i, m.group(1)))
    return out


JUDGE_SYSTEM = (
    "You are a fact-checker. Given a headline and a short story text, list ONLY specific people, teams, numbers, "
    "places or events in the text that are NOT in the headline. Ignore slang, hype phrases, greetings, jokes, "
    "opinions and generic emotions - those are fine. "
    'Return strict JSON only: {"unsupported": [...]} (empty list if nothing is invented). Max 3 items, 1-3 words each.'
)


def _is_entity(item: str, known) -> bool:
    """Keep a judge item only if it is a number or a Capitalised word that is really not in the headline."""
    s = str(item)
    if re.search(r"\d", s):
        return True
    caps = re.findall(r"\b[A-Z][a-z]{2,}\b", s)
    return any(not _is_known(c, known) for c in caps)


def judge(lines, trend, llm) -> list:
    """LLM fact-check pass. Raises on LLM errors (caller handles); junk output fails closed."""
    ctx = (trend.get("context") or {}).get("what") or ""
    user = f"Headline: {trend.get('title')}\n" + (f"Context: {ctx[:200]}\n" if ctx else "") + f"Text: {' / '.join(lines)}"
    raw = llm.chat(JUDGE_SYSTEM, user, temperature=0, max_tokens=80)
    try:
        m = re.search(r"\{.*\}", raw or "", flags=re.S)
        un = json.loads(m.group(0)).get("unsupported") if m else None
    except ValueError:
        return ["fact-check judge returned junk"]
    if not isinstance(un, list):
        return ["fact-check judge returned junk"]
    known = _known_tokens(trend)
    return [f"unsupported claim: {str(u)[:40]}" for u in un[:3] if _is_entity(u, known)]


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
        f"Language: {cfg.COPY_LANGUAGE}. Slang only as seasoning; every line must contain a fact from the headline.\n"
        f"Write {lo}-{hi} short lines (each under {cfg.line_cap(mood)} characters, count carefully)"
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


# ------------------------------------------------- free (no-LLM) auto-fixes

def _strip_quotes(lines):
    return [" ".join(_QUOTE.sub("", l).split()) for l in lines]


def _autofix(lines, trend, mood):
    """Fix cheap problems in code instead of paying for another LLM call.
    1) strip quotation marks  2) drop lines that contain an invented name (if enough lines remain)."""
    lines = [l for l in _strip_quotes(lines) if l]
    lo, _ = cfg.LINES.get(mood, (1, 3))
    bad_idx = {i for i, _w in _invented_names(lines, _known_tokens(trend))}
    if bad_idx:
        kept = [l for i, l in enumerate(lines) if i not in bad_idx]
        if len(kept) >= max(1, min(lo, 2)):
            lines = kept
    return lines


def write_one(trend, mood, angle, llm, recent=()) -> dict:
    """Returns {"ok": True, "lines": [...]} or {"ok": False, "reasons": [...]}.
    Cost per asset: 1 write call (+1 judge call only if the free checks pass); retry only when code cannot fix it."""
    feedback = []
    for _ in range(cfg.WRITE_ATTEMPTS):
        try:
            lines = _parse_lines(llm.chat(SYSTEM, _prompt(trend, mood, angle, list(recent), feedback),
                                          temperature=0.9, max_tokens=300))
            if not lines:
                feedback = ["output was not valid JSON with lines"]
                continue
            lines = _autofix(lines, trend, mood)                 # free fixes first
            problems = fact_check(lines, trend, mood)
            if not problems:
                dup = is_duplicate(lines, recent)                # free check before the paid judge
                if dup:
                    problems = [dup]
                elif USE_JUDGE:
                    problems = judge(lines, trend, llm)          # LLM fact-check, entities only
        except BudgetExceeded as exc:
            return {"ok": False, "reasons": [f"budget: {exc}"]}
        except Exception as exc:
            return {"ok": False, "reasons": [f"llm error: {type(exc).__name__}"]}
        if not problems:
            return {"ok": True, "lines": lines}
        feedback = problems
    return {"ok": False, "reasons": feedback}


# ------------------------------------------------ rejection memory (no repeat spend)

def _title_key(trend) -> str:
    return hashlib.sha1(" ".join(sorted(tokens(trend.get("title", "")))).encode("utf-8")).hexdigest()[:12]


def _rej_filter(trend, mood, angle) -> dict:
    return {"trend_id": trend["trend_id"], "mood": mood, "angle": angle, "title_key": _title_key(trend)}


def _was_rejected(db, trend, mood, angle) -> bool:
    try:
        return db[REJECTIONS].find_one(_rej_filter(trend, mood, angle)) is not None
    except Exception:
        return False


def _remember_rejection(db, trend, mood, angle, reasons):
    now = datetime.now(timezone.utc)
    try:
        db[REJECTIONS].update_one(
            _rej_filter(trend, mood, angle),
            {"$set": {"reasons": reasons[:3], "at": now, "expires_at": now + timedelta(hours=REJECTION_TTL_HOURS)}},
            upsert=True)
    except Exception:
        log.warning("could not store copy rejection", exc_info=True)


def _ensure_rejection_index(db):
    try:
        db[REJECTIONS].create_index("expires_at", expireAfterSeconds=0)
    except Exception:
        pass


def process(db, llm, limit=MAX_TRENDS_PER_RUN) -> dict:
    """Write copy for analyzed trend angles that have none yet; save as pending assets.
    Stops the whole run on the first LLM error/429 or when the per-run call budget is used up."""
    stats = {"written": 0, "rejected": 0, "skipped_known_reject": 0, "llm_calls": 0}
    if llm is None:
        return {**stats, "error": "no_llm"}
    _ensure_rejection_index(db)
    llm = _CountingLLM(llm, MAX_LLM_CALLS_PER_RUN)
    recent = recent_copies(db)
    rows = (db[TRENDS].find({"verification_status": "verified", "angles": {"$exists": True}})
            .sort("trend_score", -1).limit(limit))
    for t in rows:
        for a in t["angles"]:
            if db[ASSETS].find_one({"trend_id": t["trend_id"], "mood": a["mood"], "angle": a["angle"]}):
                continue
            if _was_rejected(db, t, a["mood"], a["angle"]):
                stats["skipped_known_reject"] += 1
                continue
            res = write_one(t, a["mood"], a["angle"], llm, recent)
            if not res["ok"]:
                reason = res["reasons"][0] if res["reasons"] else ""
                if reason.startswith(("llm error", "budget")):      # circuit breaker: do NOT retry next trend
                    stats.update(aborted=reason, llm_calls=llm.calls)
                    log.warning("writer stopped early: %s (calls=%s)", reason, llm.calls)
                    return stats
                stats["rejected"] += 1
                _remember_rejection(db, t, a["mood"], a["angle"], res["reasons"])
                log.info("copy rejected %s/%s/%s: %s", t["trend_id"], a["mood"], a["angle"], res["reasons"])
                continue
            doc = new_asset(trend_id=t["trend_id"], mood=a["mood"], angle=a["angle"], copy=res["lines"],
                            image=None, layout_id=None, layout_version=None, copy_hash=copy_hash(res["lines"]))
            db[ASSETS].insert_one(doc)          # image/layout/render are filled by steps 6-8
            recent.insert(0, res["lines"])
            stats["written"] += 1
    stats["llm_calls"] = llm.calls
    return stats
