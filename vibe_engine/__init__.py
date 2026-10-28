"""VIBE engine - personal mood Stories from the user's own photos.

Different from the auto trend engine: no news, no sources, no trend signals.
Photo-first: the photo decides the mood, the mood picks the creator personality,
and captions are small human thoughts - never headlines.
"""
from vibe_engine.pipeline import build_story

__all__ = ["build_story"]
