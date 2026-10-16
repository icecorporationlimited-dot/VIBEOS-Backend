"""Multi-step verification for trend Stories (any language, fail-closed).

A trending search is a *signal*, not proof. Before a Story is made, the item
must pass every step below; the full report is kept so you can see why a trend
was used or skipped.

  1 sources      >= N distinct publishers with https links and headlines
  2 fresh        not expired
  3 demand       approximate search volume >= threshold
  4 cross_check  the trend's key words appear in headlines of >= 2 publishers
  5 no_rumor     no rumour / hoax / "unconfirmed" language anywhere
  6 hard_block   none of: politics, courts/legal, crime accusations, war/terror,
                 communal/religion, adult, betting/finance, suicide, minors
  7 classified   (after the AI plan) classifier says not sensitive, confident
  8 confirmed    (solemn only) headlines state the loss as established fact,
                 from >= 3 publishers — death hoaxes are common

Tragedies (confirmed death of a public figure, accident, natural disaster) are
allowed ONLY as a respectful condolence Story. Everything in the hard-block list
stays blocked even then.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from trends import safety

# Words that make a loss/accident topic usable as a condolence Story.
TRAGEDY_TERMS = {
    "dies", "died", "death", "dead", "obituary", "passed away", "rip", "funeral", "demise",
    "accident", "crash", "collapse", "earthquake", "flood", "floods", "cyclone", "wildfire",
    "tragedy", "stampede", "heart attack", "cardiac", "hospitalised", "hospitalized", "cancer",
    "maut", "nidhan", "मौत", "निधन", "दुर्घटना",
}
# Extra wording only used to detect "this is a loss" (never to allow anything by itself).
LOSS_HINTS = {
    "passes away", "pass away", "passing away", "no more", "last rites", "antim sanskar", "condolence",
    "condolences", "mourn", "mourns", "mourning", "tribute", "tributes", "late actor", "late singer",
    "nahi rahe", "shraddhanjali", "shradhanjali", "alvida", "lost his life", "lost her life",
    "last breath", "breathed his last", "breathed her last", "succumbs", "succumbed",
    "श्रद्धांजलि", "नहीं रहे", "अंतिम संस्कार",
}
HARD_TERMS = [t for t in safety.SENSITIVE_TERMS if t not in TRAGEDY_TERMS]
HARD_TERMS += ["child abuse", "minor", "minors", "pocso", "self-harm", "overdose", "lynch"]

RUMOR_TERMS = [
    "rumour", "rumours", "rumor", "rumors", "hoax", "fake news", "fact check", "fact-check",
    "denies", "denied", "not dead", "alive", "unconfirmed", "reportedly", "allegedly", "alleged",
    "claims", "claimed", "speculation", "speculated", "viral video", "clarifies", "clarified",
    "afwah", "अफवाह", "fake",
]
STOPWORDS = {"the", "and", "for", "with", "from", "that", "this", "have", "has", "are", "was",
             "who", "what", "when", "how", "why", "news", "live", "today", "latest", "vs",
             "match", "update", "updates", "ka", "ki", "ke", "hai", "hain", "me", "se", "ko"}

HARD_RE = safety._compile(HARD_TERMS)
RUMOR_RE = safety._compile(RUMOR_TERMS)
TRAGEDY_RE = safety._compile(sorted(TRAGEDY_TERMS | LOSS_HINTS))


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def item_text(item: dict) -> str:
    return safety._text(item)


def publishers(item: dict) -> list[str]:
    seen = []
    for n in item.get("news") or []:
        name = str(n.get("source", "")).strip()
        if (name and n.get("title") and str(n.get("url", "")).startswith("https://")
                and name.lower() not in [s.lower() for s in seen]):
            seen.append(name)
    return seen


def key_tokens(title: str) -> list[str]:
    toks = [t for t in re.split(r"[\W_]+", title.lower(), flags=re.UNICODE) if len(t) >= 3]
    return [t for t in toks if t not in STOPWORDS]


def cross_check(item: dict) -> int:
    """How many distinct publishers carry (most of) the trend's key words in a headline."""
    toks = key_tokens(str(item.get("title", "")))
    if not toks:
        return 0
    need = max(1, (len(toks) + 1) // 2)
    hits = set()
    for n in item.get("news") or []:
        head = (str(n.get("title", "")) + " " + str(n.get("snippet", ""))).lower()
        if sum(1 for t in toks if t in head) >= need and n.get("source"):
            hits.add(str(n["source"]).strip().lower())
    return len(hits)


def has_tragedy_words(text: str) -> bool:
    return bool(TRAGEDY_RE and TRAGEDY_RE.search(text))


def _step(name, ok, detail=""):
    return {"step": name, "ok": bool(ok), "detail": str(detail)}


def pre_check(item: dict, *, blocked_topics=(), min_sources=2, min_traffic=10_000, now=None) -> list[dict]:
    """Steps 1-6 (no AI needed)."""
    now = now or datetime.now(timezone.utc)
    text = item_text(item)
    pubs = publishers(item)
    traffic = item.get("approx_traffic_numeric")
    try:
        expired = _aware(item["expires_at"]) <= now
    except (KeyError, TypeError):
        expired = True
    hard = HARD_RE.search(text) if HARD_RE else None
    extra = safety.sensitive_hit(text, blocked_topics) if blocked_topics else None
    rumor = RUMOR_RE.search(text) if RUMOR_RE else None
    return [
        _step("sources", len(pubs) >= min_sources, f"{len(pubs)} publishers: {', '.join(pubs[:4])}"),
        _step("fresh", not expired),
        _step("demand", traffic is not None and traffic >= min_traffic, f"~{traffic} searches"),
        _step("cross_check", cross_check(item) >= 2, f"{cross_check(item)} publishers match the topic"),
        _step("no_rumor", not rumor, rumor.group(0) if rumor else ""),
        _step("hard_block", not (hard or extra), (hard.group(0) if hard else extra) or ""),
    ]


def post_check(item: dict, plan: dict, *, allow_solemn=True, min_confidence=0.7, solemn_min_sources=3) -> list[dict]:
    """Steps 7-8 once the AI plan exists."""
    kind = plan.get("kind")
    steps = [_step("classified",
                   kind in safety_kinds() and plan.get("sensitive") is False
                   and float(plan.get("confidence", 0)) >= min_confidence,
                   f"kind={kind} sensitive={plan.get('sensitive')} conf={plan.get('confidence')}"),
             # A loss/accident must never get a party/hype design, whatever the model says.
             _step("tone_match", kind == "solemn" or not has_tragedy_words(item_text(item)),
                   "loss/accident wording but non-solemn design" if kind != "solemn" else "")]
    if kind == "solemn":
        confirmed = (plan.get("confirmed") is True and len(publishers(item)) >= solemn_min_sources
                     and cross_check(item) >= solemn_min_sources)
        steps.append(_step("confirmed", allow_solemn and confirmed,
                           f"confirmed={plan.get('confirmed')} publishers={len(publishers(item))}"))
    return steps


def safety_kinds():
    from creative_studio.story_themes import KINDS
    return set(KINDS)


def verdict(steps: list[dict]) -> dict:
    failed = next((s for s in steps if not s["ok"]), None)
    return {"ok": failed is None, "reason": "ok" if failed is None else f"{failed['step']}:{failed['detail']}"[:120],
            "steps": steps}
