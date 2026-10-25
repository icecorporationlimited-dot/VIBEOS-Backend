"""Pick up to 5 good, *different* photos without touching their source assets.

Stage 1: measure every candidate cheaply (no network). Stage 2: ask the vision model only about the best few
(keeps Groq calls and RAM low). Final score = measured quality, nudged by the vision model's opinion.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from . import budget
from .photo_understanding import inspect_photo, groq_describe

log = logging.getLogger(__name__)
VISION_TOP = 5          # at most this many Groq vision calls per collage
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
    top = cands[:VISION_TOP]
    if top and budget.have(60):                       # vision is optional: skip it when time is short
        with ThreadPoolExecutor(max_workers=len(top)) as pool:
            futs = {pool.submit(groq_describe, f["bytes"]): f for f in top}
            try:
                for fut in as_completed(futs, timeout=20):     # whole stage capped, not 25 s per photo
                    try:
                        futs[fut].update(fut.result())
                    except Exception:
                        log.info("collage: vision skipped for %s", futs[fut].get("asset_id"))
            except Exception:
                log.warning("collage: vision stage hit its 20s cap; continuing without the rest")
            for fut in futs:
                fut.cancel()
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
