"""ratelimit.py — per-user abuse protection for the public Telegram bot.

Purely additive: it runs in an earlier handler group and only *blocks* an update
when one user exceeds a limit. Normal usage is never affected, and no existing
handler is modified. Limits are per Telegram user, so one noisy user cannot
slow down or lock out anyone else.

Defaults (override with env vars, all "count/seconds"):
  RATE_LIMIT_COMMANDS   25/60     slash commands
  RATE_LIMIT_CALLBACKS  60/60     inline button taps
  RATE_LIMIT_PHOTOS     60/600    photos (an album of 10 is fine)
  RATE_LIMIT_MESSAGES   30/60     any other message
"""
import logging
import os
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

DEFAULTS = {
    "commands": (25, 60),
    "callbacks": (60, 60),
    "photos": (60, 600),
    "messages": (30, 60),
}
NOTICE_COOLDOWN = 30.0      # tell a blocked user at most once per this many seconds
MAX_TRACKED = 20000         # bound memory; stale entries are pruned first


def _parse(name, default):
    raw = os.environ.get(f"RATE_LIMIT_{name.upper()}", "")
    try:
        count, _, window = raw.partition("/")
        count, window = int(count), float(window)
        if count > 0 and window > 0:
            return count, window
    except ValueError:
        pass
    return default


def load_limits():
    return {k: _parse(k, v) for k, v in DEFAULTS.items()}


class RateLimiter:
    """Sliding-window limiter keyed by (user, category). Thread-safe."""

    def __init__(self, limits=None, clock=time.monotonic):
        self.limits = limits or load_limits()
        self.clock = clock
        self._hits = {}
        self._notified = {}
        self._lock = threading.Lock()

    def allow(self, user_id, category):
        """Record one event; return True if allowed, False if over the limit."""
        limit, window = self.limits.get(category, self.limits["messages"])
        now = self.clock()
        key = (str(user_id), category)
        with self._lock:
            if len(self._hits) > MAX_TRACKED:
                self._prune(now)
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] >= window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def should_notify(self, user_id, category):
        """True at most once per NOTICE_COOLDOWN for a blocked user/category."""
        now = self.clock()
        key = (str(user_id), category)
        with self._lock:
            last = self._notified.get(key)
            if last is not None and now - last < NOTICE_COOLDOWN:
                return False
            self._notified[key] = now
            return True

    def _prune(self, now):
        longest = max(w for _, w in self.limits.values())
        for key in [k for k, q in self._hits.items() if not q or now - q[-1] >= longest]:
            self._hits.pop(key, None)
        for key in [k for k, t in self._notified.items() if now - t >= NOTICE_COOLDOWN]:
            self._notified.pop(key, None)


def classify(update):
    """Return (user_id, category) for a Telegram Update, or (None, None)."""
    user = getattr(update, "effective_user", None)
    if user is None:
        return None, None
    if getattr(update, "callback_query", None) is not None:
        return user.id, "callbacks"
    message = getattr(update, "effective_message", None)
    if message is None:
        return user.id, "messages"
    if getattr(message, "photo", None):
        return user.id, "photos"
    text = getattr(message, "text", None) or ""
    if text.startswith("/"):
        return user.id, "commands"
    return user.id, "messages"


def make_gate(limiter=None):
    """Build the async gate handler. Register it in group -1 (see bot.py)."""
    limiter = limiter or RateLimiter()

    async def gate(update, context):
        from telegram.ext import ApplicationHandlerStop

        user_id, category = classify(update)
        if user_id is None or limiter.allow(user_id, category):
            return
        logger.warning("Rate limit hit: user=%s category=%s", user_id, category)
        if limiter.should_notify(user_id, category):
            try:
                if update.callback_query is not None:
                    await update.callback_query.answer(
                        "Too many requests — please slow down a bit.", show_alert=False)
                elif update.effective_message is not None:
                    await update.effective_message.reply_text(
                        "⏳ You're going a bit fast. Please wait a moment and try again.")
            except Exception:
                logger.debug("Could not send rate-limit notice", exc_info=True)
        raise ApplicationHandlerStop

    return gate
