"""Shared helpers for every discovery source: publisher names, headline grouping, signal."""
from datetime import datetime, timezone

from .. import config as cfg
from .base import candidate, similar


def canon_publisher(name) -> str:
    """One display name per publisher ('The Indian Express' == 'Indian Express')."""
    n = " ".join(str(name or "").split())
    return cfg.PUBLISHER_ALIASES.get(n.lower(), n)[:80]


def aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def group_articles(arts):
    """arts: [{title, name, url, published_at}] newest/most-prominent first.

    Similar headlines = one event; each distinct publisher becomes one source of that event.
    Returns [{"title", "rank", "sources": [...]}].
    """
    groups = []
    for rank, a in enumerate(arts):
        src = {"name": canon_publisher(a["name"]), "url": a["url"], "published_at": aware(a.get("published_at")),
               "title": str(a.get("title") or "")[:300]}      # publisher's OWN headline: used by verify cross-checks
        for g in groups:
            if similar(g["title"], a["title"]):
                if src["name"].lower() not in {s["name"].lower() for s in g["sources"]}:
                    g["sources"].append(src)
                break
        else:
            groups.append({"title": a["title"], "rank": rank, "sources": [src]})
    return groups


def groups_to_candidates(category, groups, tag):
    out = []
    for g in groups:
        signal = min(100, len(g["sources"]) * 25 + max(0, 30 - g["rank"]))
        c = candidate(category, g["title"], g["sources"], signal)
        if c:
            c["discovered_via"] = [tag]
            out.append(c)
    return out
