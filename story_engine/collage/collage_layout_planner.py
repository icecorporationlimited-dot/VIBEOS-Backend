"""Slot planner for story collages: which photo goes into which slot, and where the slots sit.

Why this exists
  The styles used to fill hard-coded slots in quality order (photo #1 -> slot #1 ...), so a portrait could land in a
  landscape slot and get cropped badly, and some slots overlapped by accident. Now every style asks this module.

What it guarantees (check_plan() verifies all of it, the validator and the tests call it)
  1. Orientation first: every photo's aspect ratio and subject_box are measured; a portrait photo gets a portrait
     slot (3:4 or 4:5), a landscape photo a landscape slot (4:3 or 5:4), a squarish one 1:1 / 4:5 / 5:4.
  2. Slot ratios are FIXED (RATIOS below) - no free-form w/h. Slots are built from rows: each row is filled to the
     same width, so the ratio of every slot is exact (rounded to whole pixels) and nothing can overlap.
  3. Every pair of slots has >= GAP (24 px; the planner uses 28) between them, in both directions.
  4. Story safe zone: nothing important in the top 250 px or bottom 250 px (Instagram profile bar / message bar).
     All slots - and the header / caption bands the styles reserve through plan_slots() - live inside
     y = 250 .. 1670. Side margin is 60 px.
  5. The subject of each photo is respected: layouts where a photo's face / subject_box would not survive the crop
     (subject_crop.plan_crop would fall back to "contain") are penalised, so they are only used when unavoidable.

Search: every split of the photos into rows (1-3 per row) x every photo order. For each (row split, order) each photo
takes the fixed ratio that suits it best; the candidate is scored on ratio mismatch, subject fit, how much of the safe
area is used, hero size (photo #1 = best quality should be the biggest) and how far the order drifted from quality
order. n <= 6, so this is a few thousand cheap evaluations.
"""
import io
import itertools
import math

W, H = 1080, 1920
SAFE_TOP = 250                  # px - no important content above this line
SAFE_BOTTOM = 250               # px - no important content below H - SAFE_BOTTOM
SAFE_Y0, SAFE_Y1 = SAFE_TOP, H - SAFE_BOTTOM          # 250 .. 1670
MARGIN_X = 60
MIN_GAP = 24                    # hard requirement
GAP = 28                        # what the planner actually uses (a little slack for rounding / shadows)

R34, R45, R11, R54, R43 = 3 / 4, 4 / 5, 1.0, 5 / 4, 4 / 3
RATIOS = {"3:4": R34, "4:5": R45, "1:1": R11, "5:4": R54, "4:3": R43}
_PORTRAIT, _SQUARE, _LANDSCAPE = "portrait", "square", "landscape"
_ALLOWED = {            # orientation of the photo -> slot ratios it may take
    _PORTRAIT: ("3:4", "4:5"),
    _SQUARE: ("1:1", "4:5", "5:4"),
    _LANDSCAPE: ("4:3", "5:4"),
}
MIN_SIDE = 280                  # a slot smaller than this is useless in a story


def orientation(aspect):
    """aspect = width / height of the (EXIF-corrected) photo."""
    return _PORTRAIT if aspect < 0.9 else _LANDSCAPE if aspect > 1.1 else _SQUARE


def photo_size(p):
    """(width, height) of a photo dict; falls back to reading the bytes when the selector did not store them."""
    if p.get("width") and p.get("height"):
        return int(p["width"]), int(p["height"])
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(p["bytes"])) as im:
        return ImageOps.exif_transpose(im).size


def _crop_ok(cache, i, ratio_name, p, size):
    """Does photo i survive a crop to this ratio (face / hard subject box fully inside)? Cached per (photo, ratio)."""
    key = (i, ratio_name)
    if key not in cache:
        from .subject_crop import plan_crop, subject_of
        r = RATIOS[ratio_name]
        cache[key] = plan_crop(size[0], size[1], (int(r * 1000), 1000), subject_of(p))["mode"] == "crop"
    return cache[key]


def _compositions(n, max_per_row=3):
    """All ways to split n photos into consecutive rows of 1..max_per_row."""
    if n == 0:
        yield ()
        return
    for k in range(1, min(max_per_row, n) + 1):
        for rest in _compositions(n - k, max_per_row):
            yield (k,) + rest


def _geometry(rows, ratios, area, gap, label_h):
    """rows = row sizes, ratios = one ratio per slot (row-major). Returns (rects, scale) with rects = [(x, y, w, h)].
    Each row is filled to the area width, so every slot has exactly its ratio; if the stack is too tall the whole
    stack is scaled down (ratios stay) and centred."""
    ax0, ay0, ax1, ay1 = area
    aw, ah = ax1 - ax0, ay1 - ay0
    heights, k = [], 0
    for cnt in rows:
        rs = ratios[k:k + cnt]; k += cnt
        heights.append((aw - gap * (cnt - 1)) / sum(rs))
    fixed = label_h * len(rows) + gap * (len(rows) - 1)          # labels and gaps never shrink (gap stays >= 24 px)
    s = max(0.05, min(1.0, (ah - fixed) / sum(heights)))
    y = ay0 + (ah - (sum(heights) * s + fixed)) / 2
    rects, k = [], 0
    for cnt, h in zip(rows, heights):
        rs = ratios[k:k + cnt]; k += cnt
        hh = h * s
        widths = [r * hh for r in rs]
        x = ax0 + (aw - (sum(widths) + gap * (cnt - 1))) / 2
        for wdt in widths:
            rects.append((int(round(x)), int(round(y)), int(round(wdt)), int(round(hh))))
            x += wdt + gap
        y += hh + label_h + gap
    return rects, s


def plan_slots(photos, header=0, caption=0, label_h=0, gap=GAP, margin_x=MARGIN_X, max_per_row=3):
    """Assign photos to slots.

    photos   : dicts as the selector returns them (best first); need `bytes` or `width`/`height`; `subject_region` optional
    header   : px reserved at the top of the safe zone (masthead / date line)
    caption  : px reserved at the bottom of the safe zone (title / hashtags)
    label_h  : extra px under EVERY row (for a per-photo caption line)
    Returns {"photos": [...in slot order], "slots": [{x,y,w,h,ratio,ratio_name,orientation,photo_aspect,photo_index}],
             "layout": "2-1-2", "safe": (x0,y0,x1,y1), "area": (x0,y0,x1,y1), "header_top": y, "caption_top": y,
             "gap": gap, "scale": s, "contain_slots": n}
    """
    n = len(photos)
    if n == 0:
        raise ValueError("no photos to place")
    sizes = [photo_size(p) for p in photos]
    aspects = [w / h for w, h in sizes]
    orients = [orientation(a) for a in aspects]
    area = (margin_x, SAFE_Y0 + header + (gap if header else 0),
            W - margin_x, SAFE_Y1 - caption - (gap if caption else 0))
    crop_cache = {}

    def best_ratio(i):
        """(ratio_name, cost) the photo takes in ANY slot position: closest fixed ratio, contain penalised."""
        best = None
        for name in _ALLOWED[orients[i]]:
            c = abs(math.log(aspects[i] / RATIOS[name]))
            c += 0.0 if _crop_ok(crop_cache, i, name, photos[i], sizes[i]) else 0.8
            if best is None or c < best[1]:
                best = (name, c)
        return best
    pref = [best_ratio(i) for i in range(n)]
    area_w, area_h = area[2] - area[0], area[3] - area[1]

    def search(min_side):
        winner = None
        for rows in _compositions(n, max_per_row):
            for perm in itertools.permutations(range(n)):          # perm[slot] = photo index
                names = [pref[i][0] for i in perm]
                rects, s = _geometry(rows, [RATIOS[nm] for nm in names], area, gap, label_h)
                if min(min(r[2], r[3]) for r in rects) < min_side:
                    continue
                mismatch = sum(pref[i][1] for i in perm)
                util = sum(r[2] * r[3] for r in rects) / (area_w * area_h)
                hero = next(r for r, i in zip(rects, perm) if i == 0)
                hero_share = hero[2] * hero[3] / sum(r[2] * r[3] for r in rects)
                drift = sum(abs(slot - i) for slot, i in enumerate(perm))
                cost = mismatch + 1.2 * (1 - util) - 0.5 * hero_share + 0.02 * drift
                if winner is None or cost < winner[0]:
                    winner = (cost, rows, perm, names, rects, s)
        return winner
    win = search(MIN_SIDE) or search(160)
    if win is None:                                                  # cannot happen for n <= 6, but never crash a render
        raise ValueError("no layout fits %d photos in the story safe zone" % n)
    _cost, rows, perm, names, rects, scale = win
    slots = []
    for k, ((x, y, w, h), i, nm) in enumerate(zip(rects, perm, names)):
        slots.append({"x": x, "y": y, "w": w, "h": h, "ratio_name": nm, "ratio": RATIOS[nm], "photo_index": k,
                      "orientation": orients[i], "photo_aspect": round(aspects[i], 3),
                      "asset_id": photos[i].get("asset_id")})
    return {"photos": [photos[i] for i in perm], "slots": slots, "layout": "-".join(str(r) for r in rows),
            "safe": (0, SAFE_Y0, W, SAFE_Y1), "area": area, "header_top": SAFE_Y0,
            "caption_top": SAFE_Y1 - caption, "gap": gap, "scale": round(scale, 3),
            "contain_slots": sum(1 for k, i in enumerate(perm) if not _crop_ok(crop_cache, i, names[k], photos[i], sizes[i]))}


def _gap_between(a, b):
    """Smallest distance between two slot rects (0 when they touch / overlap)."""
    dx = max(b["x"] - (a["x"] + a["w"]), a["x"] - (b["x"] + b["w"]), 0)
    dy = max(b["y"] - (a["y"] + a["h"]), a["y"] - (b["y"] + b["h"]), 0)
    return math.hypot(dx, dy) if dx and dy else max(dx, dy)


def check_plan(plan, min_gap=None, ratio_tol=0.012):
    """Hard rules -> list of error strings (empty = ok). Used by the validator and the tests.
    min_gap=None -> plan["min_gap"] if the style set one (playful_hype tiles touch: 0), else MIN_GAP (24)."""
    if min_gap is None:
        min_gap = plan.get("min_gap", MIN_GAP)
    errs, slots = [], plan["slots"]
    for k, s in enumerate(slots):
        if s["y"] < SAFE_Y0 or s["y"] + s["h"] > SAFE_Y1:
            errs.append(f"slot_{k}_outside_safe_zone")
        if s["x"] < 0 or s["x"] + s["w"] > W:
            errs.append(f"slot_{k}_outside_canvas")
        if s["ratio_name"] not in RATIOS:
            errs.append(f"slot_{k}_ratio_not_fixed")
        elif abs(s["w"] / s["h"] / RATIOS[s["ratio_name"]] - 1) > ratio_tol:
            errs.append(f"slot_{k}_ratio_drift")
        if s["orientation"] == _PORTRAIT and s["ratio"] > 1.0:
            errs.append(f"slot_{k}_portrait_photo_in_wide_slot")
        if s["orientation"] == _LANDSCAPE and s["ratio"] < 1.0:
            errs.append(f"slot_{k}_landscape_photo_in_tall_slot")
    for a, b in itertools.combinations(range(len(slots)), 2):
        if _gap_between(slots[a], slots[b]) < min_gap:
            errs.append(f"slots_{a}_{b}_gap_below_{min_gap}px")
    return errs


def junction_points(plan, rng=None, limit=None):
    """Free spots for stickers / doodles: the gap crossings next to slot corners. Never inside a slot, always inside
    the safe zone, so decoration never lands on a photo by accident."""
    g = plan["gap"] / 2
    pts = []
    for s in plan["slots"]:
        for px, py in ((s["x"] + s["w"] + g, s["y"] + s["h"] + g), (s["x"] - g, s["y"] + s["h"] + g)):
            if 40 <= px <= W - 40 and SAFE_Y0 + 20 <= py <= SAFE_Y1 - 20 and not any(
                    t["x"] <= px <= t["x"] + t["w"] and t["y"] <= py <= t["y"] + t["h"] for t in plan["slots"]):
                pts.append((int(px), int(py)))
    pts = sorted(set(pts))
    if rng is not None:
        rng.shuffle(pts)
    return pts[:limit] if limit else pts
