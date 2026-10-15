"""
publishing/autopilot.py — per-user autopilot rules + the /autopilot command parser.

Stored in one small document per user (collection `autopilot`). Every setter
validates, so a typo in Telegram can never leave the engine in a broken state.

  paused         emergency stop: nothing *scheduled* is published (/pause, /resume)
  enabled        autofill: the worker schedules ready photos into calendar slots
  daily_limit    max posts per local day (all formats)
  quiet_start/end  local hours with no posting, e.g. 23 -> 7
  formats        allowed formats: photo, story, carousel
  blocked_topics words/phrases that put a post on hold
  min_gap_minutes  minimum spacing between posts
  slots          preferred local posting times, "HH:MM"
"""
import re
from datetime import datetime, timezone

FORMATS = ("photo", "story", "carousel", "reel")
DEFAULTS = {
    "enabled": False,
    "full_auto": False,
    "trend_stories": "off",      # off | auto  (needs autopilot on; Story format allowed)
    "paused": False,
    "daily_limit": 2,
    "quiet_start": 23,
    "quiet_end": 7,
    "formats": ["photo", "story", "carousel"],   # reel is opt-in: /autopilot formats ... reel
    "blocked_topics": [],
    "min_gap_minutes": 60,
    "slots": ["12:30", "19:30"],
}
_SLOT = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _now():
    return datetime.now(timezone.utc)


def _int(value, lo, hi, name):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number.") from None
    if not lo <= n <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}.")
    return n


def _clean(field, value):
    if field in ("enabled", "paused", "full_auto"):
        return bool(value)
    if field == "trend_stories":
        mode = str(value).lower()
        if mode not in ("off", "auto"):
            raise ValueError("Trend stories must be: off or auto.")
        return mode
    if field == "daily_limit":
        return _int(value, 1, 10, "Daily limit")
    if field in ("quiet_start", "quiet_end"):
        return _int(value, 0, 23, "Hour")
    if field == "min_gap_minutes":
        return _int(value, 0, 720, "Gap (minutes)")
    if field == "formats":
        fmts = [str(f).lower() for f in value]
        bad = [f for f in fmts if f not in FORMATS]
        if bad or not fmts:
            raise ValueError("Formats must be from: " + ", ".join(FORMATS))
        return sorted(set(fmts), key=FORMATS.index)
    if field == "blocked_topics":
        topics = sorted({str(t).strip().lower().lstrip("#") for t in value if str(t).strip()})
        if len(topics) > 50:
            raise ValueError("At most 50 blocked topics.")
        return topics
    if field == "slots":
        out = []
        for t in value:
            m = _SLOT.match(str(t).strip())
            if not m:
                raise ValueError(f"'{t}' is not a time like 19:30.")
            out.append(f"{int(m.group(1)):02d}:{m.group(2)}")
        if not out or len(out) > 6:
            raise ValueError("Give 1 to 6 posting times.")
        return sorted(set(out))
    raise ValueError(f"Unknown setting: {field}")


class AutopilotStore:
    def __init__(self, collection, clock=_now):
        self.col, self.clock = collection, clock

    def ensure_indexes(self):
        self.col.create_index("telegram_id", unique=True)

    def get(self, telegram_id) -> dict:
        doc = self.col.find_one({"telegram_id": str(telegram_id)}) or {}
        out = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
        for k in DEFAULTS:
            if k in doc:
                out[k] = doc[k]
        return out

    def update(self, telegram_id, **fields) -> dict:
        clean = {k: _clean(k, v) for k, v in fields.items()}
        self.col.update_one(
            {"telegram_id": str(telegram_id)},
            {"$set": {**clean, "updated_at": self.clock()},
             "$setOnInsert": {"telegram_id": str(telegram_id)}},
            upsert=True)
        return self.get(telegram_id)

    def set_paused(self, telegram_id, paused: bool) -> dict:
        return self.update(telegram_id, paused=paused)

    def enabled_users(self):
        return [d["telegram_id"] for d in self.col.find({"enabled": True})]


HELP = (
    "⚙️ Autopilot commands\n"
    "/autopilot on | off — auto-schedule ready photos\n"
    "/autopilot full on | off — FULL AUTO: photos in, posts out, no approval\n"
    "/autopilot limit 2 — max posts per day\n"
    "/autopilot quiet 23 7 — no posting from 23:00 to 07:00\n"
    "/autopilot gap 60 — minutes between posts\n"
    "/autopilot slots 12:30 19:30 — preferred times\n"
    "/autopilot formats photo story carousel reel\n"
    "/autopilot trends auto | off — auto-post a Story for safe trending topics\n"
    "/autopilot block war politics — hold posts with these words\n"
    "/autopilot unblock politics"
)


def describe(s: dict) -> str:
    state = "⏸ PAUSED" if s["paused"] else ("🟢 on" if s["enabled"] else "⚪ off")
    return (
        f"🤖 Autopilot: {state}\n"
        f"• full auto (no approval): {'🟢 on' if s['full_auto'] else '⚪ off'}\n"
        f"• daily limit: {s['daily_limit']}\n"
        f"• quiet hours: {s['quiet_start']:02d}:00–{s['quiet_end']:02d}:00\n"
        f"• min gap: {s['min_gap_minutes']} min\n"
        f"• slots: {', '.join(s['slots'])}\n"
        f"• formats: {', '.join(s['formats'])}\n"
        f"• trend stories: {s['trend_stories']}\n"
        f"• blocked: {', '.join(s['blocked_topics']) or '—'}\n\n"
        "Change with /autopilot help"
    )


def handle_command(store: AutopilotStore, telegram_id, args) -> str:
    """Pure text-in/text-out handler for /autopilot (easy to test)."""
    if not args:
        return describe(store.get(telegram_id))
    cmd, rest = args[0].lower(), list(args[1:])
    try:
        if cmd in ("on", "off"):
            store.update(telegram_id, enabled=(cmd == "on"))
            return ("🟢 Autopilot on — ready photos will be scheduled into your slots."
                    if cmd == "on" else "⚪ Autopilot off — nothing new will be auto-scheduled.")
        if cmd == "full" and len(rest) == 1 and rest[0].lower() in ("on", "off"):
            on = rest[0].lower() == "on"
            if on:
                store.update(telegram_id, full_auto=True, enabled=True)
                return ("🚀 FULL AUTO on. Just send photos — I edit, write captions, "
                        "and schedule them into your slots with no approval.\n"
                        "Safety stays on: quiet hours, daily limit, blocked topics, "
                        "duplicate check. /pause stops everything.")
            store.update(telegram_id, full_auto=False)
            return "⚪ Full auto off — photos need your Approve tap again."
        if cmd == "limit" and len(rest) == 1:
            s = store.update(telegram_id, daily_limit=rest[0])
            return f"✅ Max {s['daily_limit']} post(s) per day."
        if cmd == "quiet" and len(rest) == 2:
            s = store.update(telegram_id, quiet_start=rest[0], quiet_end=rest[1])
            return f"✅ Quiet hours {s['quiet_start']:02d}:00–{s['quiet_end']:02d}:00."
        if cmd == "gap" and len(rest) == 1:
            s = store.update(telegram_id, min_gap_minutes=rest[0])
            return f"✅ At least {s['min_gap_minutes']} min between posts."
        if cmd == "slots" and rest:
            s = store.update(telegram_id, slots=rest)
            return "✅ Slots: " + ", ".join(s["slots"])
        if cmd == "trends" and len(rest) == 1 and rest[0].lower() in ("auto", "off"):
            s = store.update(telegram_id, trend_stories=rest[0].lower())
            if s["trend_stories"] == "auto":
                return ("📰 Trend Stories: auto. At most one a day, only entertainment/music/"
                        "sports/fashion/tech/festival/lifestyle topics with 2+ news sources, "
                        "never politics, courts, crime, deaths or your blocked topics. "
                        "Needs /autopilot on and the story format. /pause stops everything.")
            return "⚪ Trend Stories off."
        if cmd == "formats" and rest:
            s = store.update(telegram_id, formats=rest)
            return "✅ Allowed formats: " + ", ".join(s["formats"])
        if cmd == "block" and rest:
            cur = store.get(telegram_id)["blocked_topics"]
            s = store.update(telegram_id, blocked_topics=cur + rest)
            return "✅ Blocked: " + ", ".join(s["blocked_topics"])
        if cmd == "unblock" and rest:
            drop = {t.lower().lstrip("#") for t in rest}
            cur = [t for t in store.get(telegram_id)["blocked_topics"] if t not in drop]
            s = store.update(telegram_id, blocked_topics=cur)
            return "✅ Blocked: " + (", ".join(s["blocked_topics"]) or "—")
    except ValueError as exc:
        return f"❌ {exc}"
    return HELP
