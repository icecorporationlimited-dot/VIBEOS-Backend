"""Story-aware collage photo selection with explainable, role-balanced choices."""
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import budget
from .photo_understanding import inspect_photo, groq_describe

log = logging.getLogger(__name__)
VISION_TOP = 8
DUP_DISTANCE = 6

def _tokens(value):
    if isinstance(value, (list, tuple, set)):
        value = " ".join(map(str, value))
    return set(re.findall(r"[a-z0-9]{3,}", str(value or "").lower()))

def _context(f):
    keys = ("description", "tags", "category", "subject", "mood", "pose", "location", "outfit", "event", "caption")
    return set().union(*(_tokens(f.get(k)) for k in keys))

def _similarity(a, b):
    aa, bb = _context(a), _context(b)
    if not aa or not bb:
        # Same scheduled window is weak evidence, never enough to dominate visual/story evidence.
        sa, sb = a.get("scheduled_at"), b.get("scheduled_at")
        try:
            delta = abs((sa - sb).total_seconds())
            return 0.25 if delta <= 3 * 3600 else 0.0
        except Exception:
            return 0.0
    return len(aa & bb) / max(1, len(aa | bb))

def _clamp(v):
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0

def _final_score(f):
    # Product weights: quality 25, story relevance 25, variety 20,
    # aesthetic cohesion 15, composition/crop safety 15.
    quality = _clamp(f.get("technical_quality"))
    if f.get("vision_quality") is not None:
        quality = 0.55 * quality + 0.45 * _clamp(f.get("vision_quality"))
    relevance = _clamp(f.get("story_relevance", f.get("context_score", 0.35)))
    aesthetic = _clamp(f.get("aesthetic_match", 0.5))
    composition = _clamp(f.get("composition_score", f.get("crop_safety", 0.65)))
    f["_components"] = {"visual_quality": quality, "story_relevance": relevance,
                        "variety": 0.5, "aesthetic_match": aesthetic, "composition": composition}
    return round(.25 * quality + .25 * relevance + .20 * 0.5 + .15 * aesthetic + .15 * composition, 4)

def _dist(a, b):
    return bin(int(a) ^ int(b)).count("1")

def _role(f, index):
    if index == 0:
        return "hero_frame"
    kind = str(f.get("focus_kind", "")).lower()
    pose = str(f.get("pose", "")).lower()
    subject = str(f.get("subject", "")).lower()
    if any(x in (pose + " " + subject) for x in ("detail", "close", "accessory", "food", "object")):
        return "detail_shot"
    if any(x in pose for x in ("candid", "walking", "laugh", "action", "natural")):
        return "candid_frame"
    if f.get("wide_shot") or any(x in pose for x in ("wide", "full", "group", "landscape")):
        return "wide_or_full_shot"
    return "supporting_frame"

def select_photos(items, max_photos=5):
    cands = []
    for x in items:
        if x.get("status") not in ("ready", "scheduled") or not x.get("bytes"):
            continue
        try:
            f = inspect_photo(x["bytes"])
        except Exception:
            log.warning("collage: could not inspect %s", x.get("asset_id"), exc_info=True)
            continue
        f.update({k: x.get(k) for k in ("asset_id", "blob_id", "scheduled_at", "description", "tags",
            "category", "subject", "mood", "pose", "location", "outfit", "event", "caption", "source")})
        f["bytes"] = x["bytes"]
        # Reject only clearly unusable frames; mildly imperfect meaningful moments remain eligible.
        if f.get("width", 0) < 240 or f.get("height", 0) < 240:
            continue
        if f.get("sharpness", 0) < .025 and (f.get("brightness", 125) < 12 or f.get("brightness", 125) > 245):
            continue
        cands.append(f)
    if not cands:
        return []
    # Vision pass is deliberately broader than final count so a lower-quality candid/detail can add story value.
    vision_pool = sorted(cands, key=lambda c: c["technical_quality"], reverse=True)[:VISION_TOP]
    if vision_pool and budget.have(60):
        with ThreadPoolExecutor(max_workers=min(4, len(vision_pool))) as pool:
            futs = {pool.submit(groq_describe, f["bytes"]): f for f in vision_pool}
            try:
                for fut in as_completed(futs, timeout=20):
                    try:
                        futs[fut].update(fut.result())
                    except Exception:
                        log.info("collage: vision skipped for %s", futs[fut].get("asset_id"))
            except Exception:
                log.warning("collage: vision stage hit its 20s cap; continuing with available features")
            for fut in futs:
                fut.cancel()
    for f in cands:
        f["context_score"] = max((_similarity(f, other) for other in cands if other is not f), default=0.35)
        # vision model can explicitly report palette/mood coherence or crop safety
        if f.get("aesthetic_match") is None:
            f["aesthetic_match"] = 0.65 if f.get("mood") or f.get("colours") or f.get("colors") else 0.5
        if f.get("composition_score") is None:
            f["composition_score"] = 0.75 if f.get("focus_kind") in ("face", "saliency") else 0.6
        f["quality_score"] = _final_score(f)
    # Seed a likely event cluster; avoid combining clearly unrelated contexts.
    seed = max(cands, key=lambda f: f["quality_score"])
    related = [f for f in cands if f is seed or _similarity(seed, f) >= 0.08]
    # If metadata is sparse, keep plausible photos but prefer nearest scheduled context.
    if len(related) < 3:
        related = sorted(cands, key=lambda f: (_similarity(seed, f), f["quality_score"]), reverse=True)[:min(3, len(cands))]
    chosen = [seed]
    reasons = {seed["asset_id"]: ["hero_frame", "best_balanced_score"]}
    while len(chosen) < min(max_photos, len(related)):
        remaining = [f for f in related if f not in chosen and not any(_dist(f["hash"], c["hash"]) <= DUP_DISTANCE for c in chosen)]
        if not remaining:
            break
        def marginal(f):
            sim = max((_similarity(f, c) for c in chosen), default=0)
            # Novelty rewards different pose/shot, but contextual mismatch is penalized.
            novelty = 1.0 - max((_dist(f["hash"], c["hash"]) / 64 for c in chosen), default=0)
            context = max((_similarity(f, c) for c in chosen), default=0.35)
            return f["quality_score"] + .20 * (1.0 - novelty) + .20 * context
        nxt = max(remaining, key=marginal)
        chosen.append(nxt)
        reasons[nxt["asset_id"]] = []
    # Flexible story roles; roles are descriptive, not hard requirements when the source set lacks one.
    for i, f in enumerate(chosen):
        role = _role(f, i)
        why = reasons.setdefault(f.get("asset_id"), [])
        why.append(role)
        if f.get("vision_quality") is not None and f["vision_quality"] >= .75:
            why.append("strong_visual_quality")
        if any(_similarity(f, c) >= .15 for c in chosen if c is not f):
            why.append("matches_event_cluster")
        if all(_dist(f["hash"], c["hash"]) > DUP_DISTANCE for c in chosen if c is not f):
            why.append("adds_visual_variety")
        f["selection_role"] = role
        f["selection_reasons"] = list(dict.fromkeys(why))
        f["selection_score"] = f["quality_score"]
    for f in cands:
        if f not in chosen:
            f["bytes"] = None
    return chosen
