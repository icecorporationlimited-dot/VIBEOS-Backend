"""Pick up to 5 good, *different* photos without touching their source assets.

Stage 1: measure every candidate cheaply (no network). Stage 2: ask the vision model only about the best few
(keeps Groq calls and RAM low). Final score = measured quality, nudged by the vision model's opinion.
"""
import logging
from .photo_understanding import inspect_photo, groq_describe

log = logging.getLogger(__name__)
VISION_TOP = 8          # at most this many Groq vision calls per collage
DUP_DISTANCE = 6        # aHash Hamming distance <= this => near-duplicate


def _final_score(f):
    t = float(f.get("technical_quality", 0))
    v = f.get("vision_quality")
    return round(0.65 * t + 0.35 * float(v), 3) if v is not None else t


def _dist(a, b):
    return bin(a ^ b).count("1")


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
        f.update({k: x.get(k) for k in ("asset_id", "blob_id", "scheduled_at")})
        f["bytes"] = x["bytes"]
        cands.append(f)
    cands.sort(key=lambda c: c["technical_quality"], reverse=True)
    for f in cands[:VISION_TOP]:
        try:
            f.update(groq_describe(f["bytes"]))
        except Exception:
            log.info("collage: vision skipped for %s", f.get("asset_id"))
    for f in cands:
        f["quality_score"] = _final_score(f)
    cands.sort(key=lambda c: c["quality_score"], reverse=True)
    chosen = []
    for f in cands:                                   # best first, skip near-duplicates
        if any(_dist(f["hash"], c["hash"]) <= DUP_DISTANCE for c in chosen):
            continue
        chosen.append(f)
        if len(chosen) == max_photos:
            break
    if len(chosen) < 3:                               # not enough variety: top up with the next best
        for f in cands:
            if f not in chosen:
                chosen.append(f)
            if len(chosen) >= min(3, len(cands)):
                break
    for f in cands:                                   # free the raw bytes of the ones we dropped
        if f not in chosen:
            f["bytes"] = None
    return chosen
