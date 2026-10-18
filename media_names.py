"""media_names.py - style / size NAMES only (no Pillow, no numpy).

The web/bot service validates user input against these names without importing
editor.py (which pulls in numpy + Pillow). worker.py asserts at startup that
they match editor.STYLE_NAMES / editor.SIZES, so they cannot drift.

Six real looks + "natural" (= AUTO: the engine picks the look per photo from
the tags / the pixels). Old names keep working through ALIASES.
"""
import re

STYLES = ("natural", "y2k", "moody", "film", "clean", "golden", "mono")
SIZES = ("post", "story", "square")
AUTO = "natural"

# normalised name (lowercase, letters+digits only) -> canonical style
ALIASES = {
    # Y2K / Digicam Flash
    "baddie": "y2k", "flash": "y2k", "digicam": "y2k", "y2kflash": "y2k",
    "y2kdigicamflash": "y2k", "directflash": "y2k", "nightout": "y2k",
    # Cinematic Moody
    "cinematic": "moody", "cinematicmoody": "moody", "dramatic": "moody",
    # Soft Film / Warm Vintage
    "soft": "film", "softfilm": "film", "vintage": "film", "warmvintage": "film",
    "softfilmwarmvintage": "film", "retro": "film",
    # Clean Editorial
    "editorial": "clean", "cleaneditorial": "clean", "neutral": "clean",
    "minimal": "clean",
    # Golden Hour Glow
    "glow": "golden", "goldenhour": "golden", "goldenhourglow": "golden",
    "sunset": "golden", "sunrise": "golden",
    # Monochrome
    "bw": "mono", "monochrome": "mono", "blackwhite": "mono",
    "blackandwhite": "mono", "monochromebw": "mono",
    # Auto
    "auto": "natural", "adaptive": "natural",
}


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def resolve_style(name, default=AUTO):
    """Any known/legacy/tag name -> one of STYLES; unknown -> `default`."""
    key = _norm(name)
    if key in STYLES:
        return key
    return ALIASES.get(key, default)


def is_known(name):
    key = _norm(name)
    return key in STYLES or key in ALIASES
