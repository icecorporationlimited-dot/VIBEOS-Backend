"""trends/schema.py — one trend item = one document in `trend_items`.

Only real, sourced trends are stored (source_name + source_url/evidence). No LLM-written ideas.
"""
import hashlib
import re
from datetime import datetime, timedelta, timezone

KINDS = {"audio", "hashtag"}
SOURCE_TYPES = {"sourced"}  # AI-generated ideas were removed on purpose: real sources only


def utcnow():
    return datetime.now(timezone.utc)


def make_key(kind: str, title: str) -> str:
    norm = re.sub(r"\s+", " ", title.strip().lower())
    return hashlib.sha1(f"{kind}|{norm}".encode("utf-8")).hexdigest()


def make_item(*, kind, title, source_type, source_name, ttl_hours, confidence,
              source_url=None, evidence=None, niche_tags=None, vibe=None,
              use_case=None, audio_ok=False, rights_note=None, observed_at=None):
    """Build and validate a trend item. Raises ValueError on bad input."""
    if kind not in KINDS:
        raise ValueError(f"bad kind: {kind}")
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"bad source_type: {source_type}")
    title = (title or "").strip()
    if not title:
        raise ValueError("empty title")
    if source_type == "sourced" and not (source_url or evidence):
        raise ValueError("a sourced trend needs source_url or evidence")

    confidence = max(0.0, min(1.0, float(confidence)))

    now = observed_at or utcnow()
    return {
        "key": make_key(kind, title),
        "kind": kind,
        "title": title[:300],
        "source_type": source_type,
        "source_name": source_name,
        "source_url": source_url,
        "evidence": evidence or {},
        "observed_at": now,
        "expires_at": now + timedelta(hours=ttl_hours),
        "confidence": round(confidence, 2),
        "niche_tags": [t.lower() for t in (niche_tags or [])],
        "vibe": vibe,
        "use_case": use_case,
        # Audio is never cleared for use automatically: popularity on one
        # platform says nothing about licence/availability on Instagram.
        "rights": {"audio_ok": bool(audio_ok), "note": rights_note},
    }
