"""Pick up to 5 good, *different* photos without touching their source assets.

Stage 1: measure every candidate cheaply (no network; includes face quality). Stage 2: ask the vision model only about
the best few (keeps Groq calls and RAM low). Final score = measured quality, nudged by the vision model's opinion.
Stage 3: pick with MMR - quality minus similarity to what is already chosen - so duplicates / same-backdrop shots are
skipped and the set mixes close-ups, people and scenes.
"""
import logging, os
from concurrent.futures import ThreadPoolExecutor, as_completed
from . import budget
from .photo_understanding import inspect_photo, groq_describe, groq_caption, locate_subject

log = logging.getLogger(__name__)
VISION_TOP = 5          # at most this many Groq vision calls per collage
DUP_DISTANCE = 6        # aHash Hamming distance <= this => near-duplicate (same frame)
DHASH_DUP = int(os.getenv("COLLAGE_DHASH_DUP", "8"))                 # dHash distance <= this => near-duplicate
SAME_SCENE_DHASH = int(os.getenv("COLLAGE_SCENE_DHASH", "26"))       # dHash <= this AND same colour layout => same place/backdrop
SAME_SCENE_COLOR = float(os.getenv("COLLAGE_SCENE_COLOR", "0.07"))  # mean |colour diff| of the 4x4 layouts (0..1)
DIVERSITY = float(os.getenv("COLLAGE_DIVERSITY", "0.35"))            # 0 = pure quality order, higher = more variety
QUALITY_WINDOW = float(os.getenv("COLLAGE_QUALITY_WINDOW", "0.25"))  # never pick a photo this far below the best one
KIND_BONUS = 0.05       # small nudge so the set mixes close-up / people / scene shots


def _final_score(f):
    t = float(f.get("technical_quality", 0))
    v = f.get("vision_quality")
    return round(0.65 * t + 0.35 * float(v), 3) if v is not None else t


def _dist(a, b):
    return bin(a ^ b).count("1")


def _color_diff(a, b):
    """Mean absolute difference of two 4x4 colour layouts, 0..1. 1.0 when a signature is missing (= 'unknown, not equal')."""
    if not a or not b:
        return 1.0
    return sum(abs(x - y) for x, y in zip(a, b)) / (len(a) * 255.0)


def _is_dup(a, b):
    """Too alike to use both: the same frame (aHash / dHash) OR the same place with the same colours (different pose, same backdrop)."""
    if _dist(a["hash"], b["hash"]) <= DUP_DISTANCE:
        return True
    da, db = a.get("dhash"), b.get("dhash")
    if da is None or db is None:
        return False
    d = _dist(da, db)
    if d <= DHASH_DUP:
        return True
    return d <= SAME_SCENE_DHASH and _color_diff(a.get("color_sig"), b.get("color_sig")) <= SAME_SCENE_COLOR


def _sim(a, b):
    """0..1 how alike two photos look: half structure (dHash), half colour layout."""
    da, db = a.get("dhash"), b.get("dhash")
    d = _dist(da, db) if da is not None and db is not None else _dist(a["hash"], b["hash"])
    s_struct = 1.0 - min(d, 32) / 32.0
    s_color = 1.0 - min(_color_diff(a.get("color_sig"), b.get("color_sig")) / 0.25, 1.0)
    return 0.5 * s_struct + 0.5 * s_color


def _kind(f):
    """close-up portrait / people (smaller faces) / scene (no face): the set should mix them when it can."""
    if (f.get("face_area") or 0) >= 0.06:
        return "closeup"
    return "people" if f.get("faces") else "scene"


def _pick(pool, chosen, limit, floor=None, allow_dups=False):
    """Greedy MMR: each round take the photo with the best  (1-L)*quality - L*similarity_to_chosen (+ kind bonus).
    The first pick is simply the best-scoring photo. `floor` keeps weak photos out; duplicates are skipped unless allow_dups."""
    while len(chosen) < limit:
        kinds = {_kind(c) for c in chosen}
        best, best_val = None, None
        for f in pool:
            if f in chosen or (floor is not None and f["quality_score"] < floor):
                continue
            if not allow_dups and any(_is_dup(f, c) for c in chosen):
                continue
            sim = max((_sim(f, c) for c in chosen), default=0.0)
            val = (1 - DIVERSITY) * f["quality_score"] - DIVERSITY * sim
            if chosen and _kind(f) not in kinds:
                val += KIND_BONUS
            if best_val is None or val > best_val:
                best, best_val = f, val
        if best is None:
            return
        chosen.append(best)


def _fill_captions(chosen):
    """Every picked photo should have its own vision caption; the full describe only ran for the top few,
    so the rest get a small caption-only call (still optional: time-boxed, failures fall back later)."""
    need = [f for f in chosen if not f.get("caption") and f.get("bytes")]
    if not need or not budget.have(30):
        return
    with ThreadPoolExecutor(max_workers=len(need)) as pool:
        futs = {pool.submit(groq_caption, f["bytes"]): f for f in need}
        try:
            for fut in as_completed(futs, timeout=12):
                try:
                    futs[fut]["caption"] = fut.result()
                except Exception:
                    log.info("collage: caption skipped for %s", futs[fut].get("asset_id"))
        except Exception:
            log.warning("collage: caption stage hit its 12s cap; continuing")
        for fut in futs:
            fut.cancel()


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
    if cands:
        floor = cands[0]["quality_score"] - QUALITY_WINDOW
        _pick(cands, chosen, max_photos, floor=floor)                     # 1) good + different from each other
        want = min(3, len(cands))
        if len(chosen) < want:
            _pick(cands, chosen, want)                                    # 2) too few: drop the quality floor, still no duplicates
        if len(chosen) < want:
            _pick(cands, chosen, want, allow_dups=True)                   # 3) last resort: least-similar duplicates
    log.info("collage: picked %s", [(c.get("asset_id"), c["quality_score"], _kind(c)) for c in chosen])
    _fill_captions(chosen)
    for f in chosen:                                  # subject_box / face_box only for the picked photos (rembg is slow)
        try:
            f["subject_region"] = locate_subject(f["bytes"])
        except Exception:
            log.warning("collage: subject detection failed for %s", f.get("asset_id"), exc_info=True)
            f["subject_region"] = None                       # crop falls back to a soft default
    for f in cands:                                   # free the raw bytes of the ones we dropped
        if f not in chosen:
            f["bytes"] = None
    return chosen
