"""Step 7 — Layouts. templates/<layout_id>/{template.html, style.css, manifest.json} + metadata in se_layouts.

70-80% prebuilt templates (special=false); 20-30% AI-assisted "special" ones (special=true) that are
still plain HTML/CSS in the same folder format: AI may DESIGN them, the fixed Playwright engine (step 8) renders them.
Placeholders in template.html: {{BG}} (css background), {{LINES}} (one <p class="line"> per line), {{CREDIT}}.
"""
import html
import json
import logging
import os
import random

from .db import LAYOUTS, new_layout

log = logging.getLogger(__name__)
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")


def load_all(base=TEMPLATES_DIR) -> list:
    out = []
    for name in sorted(os.listdir(base)):
        mf = os.path.join(base, name, "manifest.json")
        if name.startswith("_") or not os.path.isfile(mf):
            continue
        m = json.load(open(mf, encoding="utf-8"))
        for f in ("template.html", "style.css"):
            if not os.path.isfile(os.path.join(base, name, f)):
                raise ValueError(f"{name}: missing {f}")
        row = dict(new_layout(layout_id=m["layout_id"], version=m["version"], categories=m["categories"],
                              moods=m["moods"], text_slots=m["text_slots"], image_slots=m["image_slots"],
                              template_path=f"templates/{name}"), special=bool(m.get("special", False)))
        row["active"] = bool(m.get("active", True))
        row["description"] = m.get("description", "")
        out.append(row)
    return out


def sync(db, base=TEMPLATES_DIR) -> int:
    """Upsert every manifest into se_layouts (idempotent). Run at worker start."""
    rows = load_all(base)
    # Disable layouts that are no longer in the active design set. Keep their records so
    # already-created assets can still render with their legacy template files.
    db[LAYOUTS].update_many({}, {"$set": {"active": False}})
    for r in rows:
        db[LAYOUTS].update_one({"layout_id": r["layout_id"]}, {"$set": r}, upsert=True)
    return sum(1 for r in rows if r.get("active", True))


def fits(layout, lines) -> bool:
    s = layout["text_slots"]["lines"]
    return len(lines) <= s["max_lines"] and all(len(l) <= s["max_chars"] for l in lines)


def candidates(db, category, mood, lines) -> list:
    return [l for l in db[LAYOUTS].find({"active": True, "categories": category, "moods": mood})
            if fits(l, lines)]


def pick(db, trend, asset) -> dict | None:
    """Choose a fitting layout, targeting 70% standard / 30% special compositions.

    The 70/30 split is a target over eligible picks, not a promise for every tiny batch.
    If a category/mood has no special candidate (e.g. a solemn tribute), use a standard
    layout instead. Special layouts are curated, flexible compositions; the existing AI
    writer still creates the copy and the fixed renderer still produces the final image.
    """
    cands = candidates(db, trend["category"], asset["mood"], asset["copy"])
    if not cands:
        return None
    from .db import ASSETS
    used = {a.get("layout_id") for a in db[ASSETS].find({"trend_id": asset["trend_id"]}, {"layout_id": 1})}
    fresh = [c for c in cands if c["layout_id"] not in used]
    pool = fresh or cands
    standard = [c for c in pool if not c.get("special", False)]
    special = [c for c in pool if c.get("special", False)]
    if standard and special:
        chosen_pool = special if random.random() < 0.30 else standard
    else:
        chosen_pool = special or standard or pool
    return random.choice(chosen_pool)


def build_html(layout, asset, base=TEMPLATES_DIR) -> str:
    """Fill a template. All text is HTML-escaped. Step 8 hands this to Playwright."""
    src = open(os.path.join(base, os.path.basename(layout["template_path"]), "template.html"), encoding="utf-8").read()
    img = asset.get("image") or {}
    bg = f"url('{img['image_url']}')" if img.get("image_url") else (img.get("background_css") or "#222")
    credit = ""
    if img.get("attribution_required") and img.get("creator"):
        credit = f"Photo: {img['creator']} / {img.get('license', '')}"
    lines = "".join(f'<p class="line">{html.escape(l)}</p>' for l in asset["copy"])
    return (src.replace("{{BG}}", html.escape(bg, quote=True)).replace("{{LINES}}", lines)
            .replace("{{CREDIT}}", html.escape(credit)))


def process(db, base=TEMPLATES_DIR) -> dict:
    """Assign layout_id + layout_version to every pending asset that has copy + image but no layout yet."""
    from .db import ASSETS, TRENDS
    stats = {"assigned": 0, "no_layout": 0}
    for a in db[ASSETS].find({"layout_id": None, "image": {"$ne": None}, "quality_status": "pending"}):
        t = db[TRENDS].find_one({"trend_id": a["trend_id"]})
        lay = pick(db, t, a) if t else None
        if not lay:
            stats["no_layout"] += 1
            continue
        db[ASSETS].update_one({"_id": a["_id"]}, {"$set": {"layout_id": lay["layout_id"], "layout_version": lay["version"]}})
        stats["assigned"] += 1
    return stats
