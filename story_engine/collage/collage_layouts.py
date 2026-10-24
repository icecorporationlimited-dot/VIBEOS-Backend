"""Theme -> layout. Template JSON holds the 5-photo slots; 3 and 4 photo collages get their own slot sets
(so a 3-photo story is not 5 slots with the bottom half empty)."""
import json, pathlib
TEMPLATES = pathlib.Path(__file__).parent / "templates"

_AUTO = {   # (x, y, w, h) on the 1080x1920 canvas, leaving room for the title (top) and hashtags (bottom)
    3: [(110, 300, 860, 560), (90, 920, 440, 640), (550, 960, 440, 600)],
    4: [(90, 300, 440, 560), (550, 340, 440, 520), (90, 940, 440, 600), (550, 910, 440, 640)],
}


def load_layout(theme, n=5):
    p = TEMPLATES / (theme + ".json")
    layout = json.loads((p if p.exists() else TEMPLATES / "photo_dump.json").read_text())
    n = max(1, int(n))
    by_count = layout.get("slots_by_count") or {}
    if str(n) in by_count:
        layout["slots"] = by_count[str(n)]
    elif n < len(layout.get("slots", [])) and n in _AUTO:
        layout["slots"] = [dict(x=x, y=y, w=w, h=h) for x, y, w, h in _AUTO[n]]
    else:
        layout["slots"] = layout.get("slots", [])[:n]
    return layout
