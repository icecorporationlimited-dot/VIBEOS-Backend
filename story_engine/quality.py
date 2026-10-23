"""Step 9 — Quality check. The ONLY place an asset becomes quality_status="ready" (everything else is "rejected").

Checks: trend still safe -> fact-check (rules + LLM judge) -> text overflow (layout limits + real render measurement)
-> image (licence record, still loads, relevant to headline via vision model) -> duplicate (copy + image/layout combo).
All checks are fail-closed: an error or a missing tool means "not ready", never "ready by default".
`vision(prompt, image_b64, temperature) -> dict` is the same callable shape as photo_judge uses; `llm.chat` as in writer.
"""
import base64
import json
import logging
import hashlib
import re
from datetime import datetime, timedelta, timezone

from . import config as cfg
from . import layouts, safety, verify, writer
from .db import ASSETS, LAYOUTS, TRENDS
from .visuals import REQUIRED, license_ok

log = logging.getLogger(__name__)

# A reason starting with RETRY is an infrastructure problem (no vision key, provider down, network blip), not a
# verdict on the story. Such assets stay "pending" and are checked again next pass instead of being rejected
# for good. Only a missing vision model is not counted as an attempt (it is a config problem, not the asset's).
RETRY = "retry: "
NO_VISION = RETRY + "no vision model configured (set OPENROUTER_API_KEY or GROQ_API_KEY)"
MAX_ATTEMPTS = 3
MAX_IMAGE_BYTES = 12 * 1024 * 1024

RELEVANCE_PROMPT = (
    "Headline: {title}\nDoes this photo suit a social story about this headline? It must not mislead (wrong event, "
    "wrong people, unrelated subject) and must not look disturbing or inappropriate next to the text. "
    'Return strict JSON only: {{"relevant": true|false, "reason": "short"}}'
)


def check_trend(trend) -> list:
    if not trend:
        return ["trend missing"]
    if trend.get("verification_status") != "verified":
        return [f"trend not verified ({trend.get('verification_status')})"]
    exp = trend.get("expires_at")
    if exp and (exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)) < datetime.now(timezone.utc):
        return ["trend expired"]
    # The stored status can be hours old; sources / headlines may have changed since. Re-run the live gate.
    g = verify.gate(trend)
    if g["status"] != "verified":
        return [f"trend fails the safety gate now ({g['status']}): {(g['reasons'] or ['?'])[0]}"]
    return []


def check_safety(asset, trend) -> list:
    """Sensitive wording anywhere the reader can see it: the written copy and the context the LLM added."""
    ctx = trend.get("context") or {}
    texts = list(asset.get("copy") or []) + [str(ctx.get("what") or "")] + [str(w) for w in ctx.get("who") or []]
    hit = safety.reject_hit(*texts)
    return [f"sensitive wording in story: {hit}"] if hit else []


def check_consistency(db, trend, llm) -> list:
    """Do the publishers' own headlines agree with each other? (Grouping is by word overlap, so 'India beat
    Australia' and 'Australia beat India' land in one group.) One LLM call per trend, cached on the trend."""
    heads = safety.source_headlines(trend)
    if len({p.lower() for p, _ in heads}) < 2:
        return []                                       # calendar events / nothing to compare: verify.gate decides
    if llm is None:
        return ["no llm for consistency judge"]
    key = hashlib.sha1("|".join(sorted(f"{p}:{t}" for p, t in heads)).encode("utf-8")).hexdigest()
    cached = trend.get("consistency") or {}
    if cached.get("key") == key:
        return [] if cached.get("ok") else [f"sources disagree: {cached.get('why', '')[:80]}"]
    try:
        raw = llm.chat(safety.CONSISTENCY_SYSTEM, safety.consistency_user(trend.get("title"), heads),
                       temperature=0, max_tokens=200)
        m = re.search(r"\{.*\}", raw or "", flags=re.S)
        data = json.loads(m.group(0)) if m else None
        verdict = data.get("consistent") if isinstance(data, dict) else None
        ok = verdict is True or str(verdict).strip().lower() == "true"
        if not isinstance(data, dict) or verdict is None:
            return [RETRY + "source consistency judge returned junk"]
    except Exception:
        return [RETRY + "source consistency judge failed"]
    why = "; ".join(str(c)[:60] for c in (data.get("conflicts") or [])[:2])
    try:
        db[TRENDS].update_one({"trend_id": trend["trend_id"]}, {"$set": {"consistency": {
            "key": key, "ok": ok, "why": why, "checked_at": datetime.now(timezone.utc)}}})
    except Exception:
        log.exception("could not cache consistency verdict for %s", trend.get("trend_id"))
    return [] if ok else [f"sources disagree: {why[:80]}"]


def check_facts(asset, trend, llm) -> list:
    bad = writer.fact_check(asset["copy"], trend, asset["mood"])
    if not bad:
        if llm is None:
            return ["no llm for fact-check judge"]
        bad = writer.judge(asset["copy"], trend, llm)
    return bad


def check_overflow(asset, layout) -> list:
    if not layout:
        return ["layout missing"]
    bad = [] if layouts.fits(layout, asset["copy"]) else ["copy exceeds layout max_lines/max_chars"]
    meta = asset.get("render_meta")
    if not meta:
        bad.append("no render measurement")
    elif meta.get("overflow"):
        bad.append("text overflows the card")
    if not asset.get("rendered_image_url"):
        bad.append("not rendered")
    return bad


def _fetch(http, url):
    if http is None:
        import requests as http
    r = http.get(url, timeout=15, headers={"User-Agent": cfg.HTTP_UA})    # Wikimedia refuses the default UA
    r.raise_for_status()
    ctype = (r.headers.get("Content-Type") or "").lower()
    if not ctype.startswith("image/"):
        raise ValueError(f"not an image: {ctype}")
    if len(r.content) > MAX_IMAGE_BYTES:
        raise ValueError("image too large")
    return r.content


def check_image(asset, trend, http=None, vision=None) -> list:
    img = asset.get("image")
    if not img:
        return ["no image"]
    if not img.get("image_url"):                       # generic gradient background: nothing to verify
        return []
    bad = [f"image record missing {k}" for k in REQUIRED if k != "attribution_required" and not img.get(k)]
    if img.get("attribution_required") is None:
        bad.append("image record missing attribution_required")
    if not license_ok(img):
        bad.append("licence not allowed")
    if bad:
        return bad
    try:
        data = _fetch(http, img["image_url"])          # also proves the photo still loads
    except ValueError as exc:                          # served something that is not a usable image: final
        return [f"image does not load: {exc}"]
    except Exception as exc:                           # network / HTTP error: may be temporary
        return [f"{RETRY}image does not load: {type(exc).__name__}"]
    if not vision:
        return [NO_VISION] if cfg.QC_REQUIRE_VISION else []
    try:
        res = vision(RELEVANCE_PROMPT.format(title=trend["title"]), base64.b64encode(data).decode(), 0)
        if isinstance(res, str):
            res = json.loads(res)
        relevant, reason = res.get("relevant"), str(res.get("reason", ""))[:60]
    except Exception:
        return [f"{RETRY}image relevance check failed"]
    if relevant is True or str(relevant).strip().lower() == "true":     # some models answer "true" as text
        return []
    return [f"image not relevant: {reason}"]


def check_duplicates(db, asset, now=None) -> list:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=cfg.DUP_LOOKBACK_DAYS)
    others = [o for o in db[ASSETS].find({"quality_status": "ready", "created_at": {"$gt": since}})
              if o["_id"] != asset["_id"]]
    bad = []
    dup = writer.is_duplicate(asset["copy"], [o["copy"] for o in others if o.get("copy")])
    if dup:
        bad.append(dup)
    url = (asset.get("image") or {}).get("image_url")
    if url and any(o["trend_id"] == asset["trend_id"] and o.get("layout_id") == asset.get("layout_id")
                   and (o.get("image") or {}).get("image_url") == url for o in others):
        bad.append("same image + layout already used for this trend")
    return bad


def evaluate(db, asset, llm=None, vision=None, http=None) -> list:
    trend = db[TRENDS].find_one({"trend_id": asset["trend_id"]})
    layout = db[LAYOUTS].find_one({"layout_id": asset.get("layout_id")})
    reasons = check_trend(trend) or check_safety(asset, trend)
    if reasons:
        return reasons                                  # no point checking the rest
    return (check_facts(asset, trend, llm) + check_consistency(db, trend, llm) + check_overflow(asset, layout)
            + check_image(asset, trend, http, vision) + check_duplicates(db, asset))


def process(db, llm=None, vision=None, http=None, limit=None) -> dict:
    """pending -> ready | rejected, or stays pending when the only problem is temporary (see RETRY)."""
    stats = {"ready": 0, "rejected": 0, "retry": 0}
    rows = db[ASSETS].find({"quality_status": "pending", "rendered_image_url": {"$ne": None}})
    for a in list(rows)[: limit or cfg.QC_BATCH]:
        now = datetime.now(timezone.utc)
        try:
            reasons = evaluate(db, a, llm, vision, http)
        except Exception:
            log.exception("quality check crashed for %s", a.get("_id"))
            reasons = ["quality check crashed"]
        if reasons and all(r.startswith(RETRY) for r in reasons):
            config_issue = reasons == [NO_VISION]
            attempts = a.get("quality_attempts", 0) + (0 if config_issue else 1)
            if config_issue:
                log.warning("quality: %s; photo stories stay pending", NO_VISION[len(RETRY):])
            if config_issue or attempts < MAX_ATTEMPTS:
                db[ASSETS].update_one({"_id": a["_id"]}, {"$set": {
                    "quality_attempts": attempts, "quality_reasons": reasons, "quality_checked_at": now}})
                stats["retry"] += 1
                continue
            reasons = reasons + [f"gave up after {MAX_ATTEMPTS} attempts"]
        reasons = [r[len(RETRY):] if r.startswith(RETRY) else r for r in reasons]
        ok = not reasons
        db[ASSETS].update_one({"_id": a["_id"]}, {"$set": {
            "quality_status": "ready" if ok else "rejected", "quality_reasons": reasons,
            "quality_checked_at": now}})
        stats["ready" if ok else "rejected"] += 1
        if not ok:
            log.info("asset %s rejected: %s", a["_id"], reasons)
    return stats
