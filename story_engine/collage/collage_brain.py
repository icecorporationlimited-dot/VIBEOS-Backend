"""Theme decision, plus a trend only when it is verified AND relevant to the photos."""
from .live_trends import scan_verified_trends

MIN_RELEVANCE = 0.6


_RULES = (   # first match wins; keywords are looked up in the vision model's subject/mood/category text
    ("streetwear_baddie", "fashion", ("fashion", "outfit", "streetwear", "sneaker", "hoodie", "fit check", "urban", "street")),
    ("kpop_editorial", "music", ("concert", "music", "idol", "stage", "kpop", "k-pop", "singer", "band", "dance")),
    ("playful_hype", "lifestyle", ("festival", "celebration", "party", "birthday", "cricket", "hype", "fun", "colourful", "colorful", "playful")),
    ("classy_editorial", "general", ("nature", "minimal", "aesthetic", "calm", "elegant", "portrait", "architecture", "neutral", "landscape")),
    ("photo_dump", "lifestyle", ("friends", "selfie", "camera", "trip", "travel", "food", "cafe", "casual", "candid")),
)
_VARIETY = (("photo_dump", "lifestyle"), ("scrapbook_chaos", "lifestyle"), ("playful_hype", "lifestyle"), ("classy_editorial", "general"))


def choose_theme(photos, forced=None):
    """All six styles are reachable: keyword rules first, otherwise a stable pick by photo ids (variety, not always one look)."""
    from .collage_styles import THEMES
    if forced in THEMES:
        cat = {"streetwear_baddie": "fashion", "kpop_editorial": "music", "classy_editorial": "general"}.get(forced, "lifestyle")
        return forced, cat
    text = " ".join(str(p.get(k, "")) for p in photos for k in ("description", "tags", "category", "subject", "mood", "pose", "location")).lower()
    hits = [(sum(text.count(w) for w in words), theme, cat) for theme, cat, words in _RULES]
    best = max(hits, key=lambda h: h[0])
    if best[0] > 0:
        return best[1], best[2]
    import hashlib
    h = int(hashlib.sha256("".join(str(p.get("asset_id")) for p in photos).encode()).hexdigest(), 16)
    return _VARIETY[h % len(_VARIETY)]


def trend_context(category):
    try:
        for t in scan_verified_trends(category):
            if t["relevance"] >= MIN_RELEVANCE:
                return {"term": t["term"], "verified": True, "verification": t["verification"],
                        "sources": t["sources"], "last_verified_at": t["last_verified_at"],
                        "relevance": t["relevance"], "trend_id": t["trend_id"],
                        "trends": [{"term": t["term"], "sources": t["sources"]}]}   # what the copywriter reads
    except Exception:
        pass
    return {"term": None, "verified": False, "fallback": "evergreen", "category": category, "trends": []}
