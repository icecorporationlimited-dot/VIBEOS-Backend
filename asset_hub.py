"""asset_hub.py - ONE shared asset lookup for every render engine.

The collage engine (story_engine/collage) and the vibe engine (vibe_engine)
keep their own looks, but they no longer own private folders: any asset
uploaded through the Asset Studio - assets/vibe/... or assets/collage/... -
is visible to BOTH engines, and each engine just asks for what it needs.

Every lookup walks an ordered pool list and returns None (images) or falls
back to a system / PIL default (fonts), so a checkout with zero assets
renders exactly like before. Callers can pass first_dir= to try their own
pool first: the collage renderer does, so its look keeps priority while it
still sees new uploads (and the vibe engine sees the collage pool too).

Pools per kind (first hit wins; env overrides VIBE_ASSETS / COLLAGE_ASSETS
keep working and replace the default base dirs):
    fonts    assets/vibe/fonts -> assets/collage/fonts -> <repo>/fonts -> system
    stickers assets/vibe/stickers -> assets/collage/stickers
    textures assets/vibe/textures -> assets/collage/papers
    frames   assets/vibe/frames
    papers   assets/collage/papers -> assets/vibe/textures
    tapes    assets/collage/tapes
    scraps   assets/collage/scraps

list_names(kind) is manifest-driven (the Asset Studio maintains
assets/<category>/manifest.json) with a directory scan as the source of truth.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageFont

ROOT = Path(__file__).resolve().parent

FONT_EXTS = (".ttf", ".otf")
IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")
KIND_EXTS = {"fonts": FONT_EXTS, "stickers": IMG_EXTS, "textures": IMG_EXTS,
             "frames": IMG_EXTS, "papers": IMG_EXTS, "tapes": IMG_EXTS,
             "scraps": IMG_EXTS}

_SYSTEM_FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_EMOJI_SYSTEM = ("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
                 "/usr/share/fonts/noto/NotoColorEmoji.ttf",
                 "/usr/share/fonts/truetype/noto-color-emoji/NotoColorEmoji.ttf")


def _base(env, default):
    value = os.getenv(env)
    return Path(value) if value else ROOT / "assets" / default


def _pools():
    vibe, collage = _base("VIBE_ASSETS", "vibe"), _base("COLLAGE_ASSETS", "collage")
    return {
        "fonts": (vibe / "fonts", collage / "fonts", ROOT / "fonts"),
        "stickers": (vibe / "stickers", collage / "stickers"),
        "textures": (vibe / "textures", collage / "papers"),
        "frames": (vibe / "frames",),
        "papers": (collage / "papers", vibe / "textures"),
        "tapes": (collage / "tapes",),
        "scraps": (collage / "scraps",),
    }


POOLS = _pools()


# ---------------------------------------------------------------- lookup
@lru_cache(maxsize=256)
def _find(kind, filename, first):
    """First existing path for filename in [first] + POOLS[kind]. Cached."""
    filename = Path(str(filename).strip().replace("\\", "/")).name
    exts = KIND_EXTS.get(kind, IMG_EXTS)
    stem = Path(filename).stem
    dirs = ((Path(first),) if first else ()) + POOLS.get(kind, ())
    has_ext = Path(filename).suffix.lower() in exts
    for d in dirs:
        for name in (filename, *(stem + e for e in exts) if not has_ext else ()):
            p = d / name
            if p.is_file():
                return p
        lower = filename.lower()
        try:
            for q in d.iterdir():
                if q.is_file() and q.name.lower() == lower:
                    return q
        except OSError:
            pass
    return None


def find(kind, name, first_dir=None):
    """Path of <name> in the pools for <kind>, or None."""
    return _find(kind, name, str(first_dir) if first_dir else None)


@lru_cache(maxsize=64)
def _open(path_str):
    try:
        im = Image.open(path_str)
        im.load()
        return im.convert("RGBA") if im.mode not in ("RGBA", "L") else im
    except Exception:
        return None


def load_rel(rel, first_dir=None):
    """CACHED converted image for '<folder>/<file>' (kind = folder), or None. No copy."""
    kind, _, filename = str(rel).strip().replace("\\", "/").partition("/")
    if not kind or not filename:
        return None
    p = _find(kind, filename, str(first_dir) if first_dir else None)
    return _open(str(p)) if p else None


def load_image(kind, name, first_dir=None):
    """Copy (RGBA / L) of an image asset, or None when missing."""
    im = load_rel(kind + "/" + Path(str(name)).name, first_dir)
    return im.copy() if im is not None else None


# ---------------------------------------------------------------- fonts
def _apply_variation(font, size, weight):
    """Variable Google fonts (Caveat, Playfair, Inter, ...): set the weight axis
    and the optical size to the pixel size. Silently ignored by fixed fonts."""
    try:
        axes = font.get_variation_axes()
    except Exception:
        return
    vals = []
    for a in axes:
        nm = a["name"]
        nm = nm.decode("latin-1") if isinstance(nm, bytes) else str(nm)
        nm = nm.lower()
        if "weight" in nm and weight:
            v = weight
        elif "optical" in nm or nm == "opsz":
            v = size
        else:
            v = a["default"]
        vals.append(min(max(v, a["minimum"]), a["maximum"]))
    try:
        font.set_variation_by_axes(vals)
    except Exception:
        pass


def load_font(candidates, size, weight=None, first_dir=None):
    """First existing font of the candidate chain (variable axes applied),
    then DejaVu, then PIL's default. Never raises."""
    if isinstance(candidates, str):
        candidates = (candidates,)
    for name in candidates:
        p = _find("fonts", name, str(first_dir) if first_dir else None)
        if p:
            try:
                f = ImageFont.truetype(str(p), size)
                _apply_variation(f, size, weight)
                return f
            except Exception:
                continue
    try:
        return ImageFont.truetype(_SYSTEM_FALLBACK, size)
    except Exception:
        return ImageFont.load_default()


@lru_cache(maxsize=1)
def emoji_font_path():
    """First NotoColorEmoji in the shared pools, else the system copies, else None."""
    p = _find("fonts", "NotoColorEmoji.ttf")
    if p:
        return str(p)
    return next((s for s in _EMOJI_SYSTEM if Path(s).is_file()), None)


# ---------------------------------------------------------------- listing
def _manifest_names(kind):
    names = set()
    for env, default in (("VIBE_ASSETS", "vibe"), ("COLLAGE_ASSETS", "collage")):
        try:
            m = json.loads((_base(env, default) / "manifest.json").read_text("utf-8"))
            names.update(Path(str(n)).stem for n in (m.get(kind) or []))
        except Exception:
            continue
    return names


def list_names(kind):
    """All asset names (stems) of a kind across BOTH pools: the Asset Studio
    manifest first, directory scan as the source of truth."""
    names = _manifest_names(kind)
    exts = KIND_EXTS.get(kind, IMG_EXTS)
    for d in POOLS.get(kind, ()):
        try:
            names.update(q.stem for q in d.iterdir()
                         if q.is_file() and q.suffix.lower() in exts)
        except OSError:
            pass
    return sorted(names)
