"""media_names.py — style / size NAMES only (no Pillow, no numpy).

The web/bot service validates user input against these names without importing
editor.py (which pulls in numpy + Pillow + optional OpenCV). worker.py asserts
at startup that they match editor.STYLES / editor.SIZES, so they cannot drift.
"""
STYLES = ("natural", "baddie", "soft", "moody", "golden", "clean", "film", "mono")
SIZES = ("post", "story", "square")
