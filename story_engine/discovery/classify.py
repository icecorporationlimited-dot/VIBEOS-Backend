"""Headline -> category for sources that have no category of their own (search trends, general news)."""
import re

from .. import config as cfg

_RX = {cat: re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b", re.I)
       for cat, words in cfg.CLASSIFY_KEYWORDS.items()}


def classify(text, default="news") -> str:
    best, best_n = default, 0
    for cat in cfg.CLASSIFY_KEYWORDS:              # config order = tie-break priority
        n = len(_RX[cat].findall(text or ""))
        if n > best_n:
            best, best_n = cat, n
    return best
