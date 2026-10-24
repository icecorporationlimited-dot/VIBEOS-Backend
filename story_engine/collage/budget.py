"""Shared wall-clock budget for one collage job (worker kills the child at TIMEOUTS['collage_story'])."""
import logging
import os
import time
from contextlib import contextmanager

log = logging.getLogger("vibeos.collage")
TOTAL = float(os.getenv("COLLAGE_BUDGET_SECONDS", "120"))   # must stay below worker.TIMEOUTS['collage_story']
_start = time.monotonic()


def reset():
    global _start
    _start = time.monotonic()


def elapsed():
    return time.monotonic() - _start


def remaining():
    return TOTAL - elapsed()


def have(seconds):
    """True if at least `seconds` of budget are left."""
    return remaining() >= seconds


@contextmanager
def stage(name):
    t = time.monotonic()
    try:
        yield
    finally:
        log.info("collage stage %-10s %.1fs (total %.1fs)", name, time.monotonic() - t, elapsed())
