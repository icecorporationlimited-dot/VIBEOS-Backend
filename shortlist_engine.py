"""shortlist_engine.py - smart shortlist selection (pure: no DB, no network).

Replaces the old "sort by score, greedily skip near-dhash copies" loop with:

  1. Near-duplicate / burst CLUSTERING using three independent signals (dHash, DCT
     pHash, colour signature) so crops, re-compressions and burst shots group together
     but two different photos that merely share a hash neighbourhood do not.
  2. BEST-OF-BURST choice inside each cluster (sharper, eyes open, smiling wins).
  3. DIVERSITY-AWARE ORDERING (MMR): the shortlist is ordered so that strong *and*
     different photos come first - a feed of 5 near-identical selfies never wins over
     a varied set. The order is returned so the pipeline can prepare/post the best
     and most varied photos first.
  4. Cross-batch protection: never re-select something nearly identical to an asset that
     is already selected / ready.

Input candidates are plain dicts: {"id": ..., "analysis": {...}} where analysis comes
from media_quality.analyze_image (old assets with only a dHash still work).
"""
from __future__ import annotations

import photo_intel

DUPLICATE_DISTANCE = 6          # dHash hamming <= this -> same picture
PHASH_DUPLICATE = 6             # pHash hamming <= this -> same picture
SIMILAR_DISTANCE = 12           # dHash <= this (+ colour agreement) -> same moment
PHASH_SIMILAR = 14
HIST_AGREE = 0.62               # colour-signature agreement needed to call it "similar"
MMR_LAMBDA = 0.78               # 1.0 = pure score order, lower = more diversity
USED_PENALTY = 0.5              # weight of similarity to already-posted photos in MMR


def _dh(analysis):
    return (analysis or {}).get("dhash")


def _ph(analysis):
    return (analysis or {}).get("phash") or ((analysis or {}).get("deep") or {}).get("phash")


def _hist(analysis):
    return ((analysis or {}).get("deep") or {}).get("hist")


def relation(a: dict, b: dict) -> str:
    """'duplicate' | 'similar' | 'different' between two analysis dicts."""
    dd = photo_intel.hamming_hex(_dh(a), _dh(b))
    pd = photo_intel.hamming_hex(_ph(a), _ph(b))
    hs = photo_intel.hist_similarity(_hist(a), _hist(b))

    if dd is not None and dd <= DUPLICATE_DISTANCE:
        return "duplicate"
    if pd is not None and pd <= PHASH_DUPLICATE:
        return "duplicate"
    near = False
    if pd is not None and pd <= PHASH_SIMILAR:
        near = hs is None or hs >= HIST_AGREE
    if dd is not None and dd <= SIMILAR_DISTANCE:
        near = near or hs is None or hs >= HIST_AGREE
    return "similar" if near else "different"


def similarity(a: dict, b: dict) -> float:
    """Continuous 0..1 similarity used for diversity ordering."""
    ds = [d for d in (photo_intel.hamming_hex(_dh(a), _dh(b)),
                      photo_intel.hamming_hex(_ph(a), _ph(b))) if d is not None]
    hs = photo_intel.hist_similarity(_hist(a), _hist(b))
    if not ds and hs is None:
        return 0.0
    struct = 1.0 - min(32, min(ds)) / 32.0 if ds else None
    if struct is None:
        return float(hs)
    if hs is None:
        return struct
    return 0.65 * struct + 0.35 * hs


def _score(analysis: dict) -> float:
    try:
        return float((analysis or {}).get("quality_score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def plan_shortlist(candidates: list[dict], used: list[dict], n: int) -> dict:
    """Decide what to select from one batch.

    candidates: [{"id", "analysis"}]; used: analysis dicts of assets already
    selected/ready in other batches. Returns
      selected   ids in recommended (diversity-aware) order, len <= n
      rejected   [(id, reason)]  exact duplicates (never post twice)
      skipped    [(id, reason)]  valid but not chosen
      info       {id: {"rank", "cluster_size", "burst_winner", "mmr"}}
    """
    n = max(0, int(n))
    rejected, skipped, info = [], [], {}

    # ---- 1. drop copies of already-used photos ----------------------------------
    alive = []
    for c in candidates:
        if any(relation(c["analysis"], u) == "duplicate" for u in used):
            rejected.append((c["id"], "duplicate_of_used"))
        else:
            alive.append(c)

    # ---- 2. cluster the batch (union-find over duplicate/similar relations) ---------
    parent = list(range(len(alive)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(alive)):
        for j in range(i + 1, len(alive)):
            if relation(alive[i]["analysis"], alive[j]["analysis"]) != "different":
                parent[find(i)] = find(j)
    clusters: dict[int, list[int]] = {}
    for i in range(len(alive)):
        clusters.setdefault(find(i), []).append(i)

    # ---- 3. best frame of each cluster ----------------------------------------------
    champions = []
    for members in clusters.values():
        def key(i):
            an = alive[i]["analysis"]
            return photo_intel.burst_rank(an.get("deep"), _score(an))
        members.sort(key=key, reverse=True)
        champ = members[0]
        champions.append(champ)
        info[alive[champ]["id"]] = {"cluster_size": len(members),
                                    "burst_winner": len(members) > 1}
        for pos, i in enumerate(members[1:], start=1):
            # an exact copy of a better frame is rejected (never post it twice);
            # a same-moment alternate is merely skipped
            exact = any(relation(alive[i]["analysis"], alive[k]["analysis"]) == "duplicate"
                        for k in members[:pos])
            if exact:
                rejected.append((alive[i]["id"], "duplicate_in_batch"))
            else:
                skipped.append((alive[i]["id"], "too_similar_to_selected"))

    # ---- 4. diversity-aware ordering (MMR) ---------------------------------------------
    pool = list(champions)
    chosen: list[int] = []
    while pool and len(chosen) < n:
        best_i, best_val = None, None
        for i in pool:
            an = alive[i]["analysis"]
            rel_score = _score(an) / 100.0
            sim_sel = max((similarity(an, alive[k]["analysis"]) for k in chosen), default=0.0)
            sim_used = max((similarity(an, u) for u in used), default=0.0)
            penalty = max(sim_sel, USED_PENALTY * sim_used)
            val = MMR_LAMBDA * rel_score - (1 - MMR_LAMBDA) * penalty
            if best_val is None or val > best_val:
                best_i, best_val = i, val
        chosen.append(best_i)
        pool.remove(best_i)
        info[alive[best_i]["id"]].update({"rank": len(chosen), "mmr": round(best_val, 3)})
    for i in pool:
        skipped.append((alive[i]["id"], "not_in_top_n"))

    return {"selected": [alive[i]["id"] for i in chosen], "rejected": rejected,
            "skipped": skipped, "info": info}
