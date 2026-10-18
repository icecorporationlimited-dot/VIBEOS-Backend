"""limits.py — one shared gate for memory-heavy work (photo edits, Reel renders).

Render's small plans have 512 MB RAM. A single photo edit can briefly need a few
hundred MB, so several at once crash the service. Everything heavy goes through
heavy_slot(): extra requests simply wait their turn instead of killing the app.

Env:
  HEAVY_CONCURRENCY  how many heavy jobs may run at once (default 1)
"""
import os
import threading
from contextlib import contextmanager


def _slots():
    try:
        return max(1, int(os.environ.get("HEAVY_CONCURRENCY", "1")))
    except ValueError:
        return 1


_GATE = threading.BoundedSemaphore(_slots())


def _release_memory():
    """Hand freed memory back to the OS (glibc only; harmless no-op elsewhere)."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


@contextmanager
def heavy_slot():
    _GATE.acquire()
    try:
        yield
    finally:
        _release_memory()
        _GATE.release()
