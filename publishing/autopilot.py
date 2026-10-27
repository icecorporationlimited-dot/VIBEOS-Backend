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

from .identity import Principal, document_principal, owner_filter

FORMATS = ("photo", "story", "carousel", "reel")
DEFAULTS = {
    "enabled": False,
    "full_auto": False,
    "trend_stories": "off",      # off | auto  (needs autopilot on; Story format allowed)
    "auto_replies": False,       # AI replies to comments on the user's own media
    "auto_comments": False,      # AI adds a starter comment to newly published posts
    "paused": False,
    "daily_limit": 2,
    "quiet_start": 23,
    "quiet_end": 7,
    "formats": ["photo", "story", "carousel"],   # reel is opt-in: /autopilot formats ... reel
    "blocked_topics": [],
    "min_gap_minutes": 60,
    "slots": ["12:30", "19:30"],
    "timezone": "",              # IANA name; "" = use the owner's default (web users set it)
    "vibe": "auto",             # caption vibe preset: auto | aesthetic | desi | funny | minimal
    "occasions": {},             # context engine preferences (context_engine.prefs)
}
_SLOT = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


VIBES = ("auto", "aesthetic", "desi", "funny", "minimal")
# Vibe presets: web chips + /autopilot vibe. Each maps to the caption style +
# language the copywriter already understands, so the AI writes accordingly.
VIBE_PRESETS = {
    "aesthetic": {"caption_style": "soft", "language": "english",
                  "blurb": "dreamy, minimal, aesthetic captions in English"},
    "desi": {"caption_style": "natural", "language": "hinglish",
             "blurb": "casual Hinglish captions — desi creator energy"},
    "funny": {"caption_style": "funny", "language": "english",
              "blurb": "witty one-liners with a light punchline"},
    "minimal": {"caption_style": "clean", "language": "english",
                "blurb": "short, polished, confident captions"},
}


def vibe_caption_kwargs(settings: dict) -> dict:
    """Resolve the stored vibe preset into copywriter.build_prompt() kwargs."""
    preset = VIBE_PRESETS.get(str((settings or {}).get("vibe") or "auto"))
    if not preset:
        return {"mode": "natural", "vibe": "natural", "language": "english"}
    style = preset["caption_style"]
    return {"mode": style, "vibe": style, "language": preset["language"]}


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
    if field in ("enabled", "paused", "full_auto", "auto_replies", "auto_comments"):
        return bool(value)
    if field == "trend_stories":
        mode = str(value).lower()
        if mode not in ("off", "auto"):
            raise ValueError("Trend stories must be: off or auto.")
        return mode
    if field == "vibe":
        name = str(value or "").lower().strip()
        if name not in VIBES:
            raise ValueError("Vibe must be one of: " + ", ".join(VIBES))
        return name
    if field == "occasions":
        from context_engine.prefs import clean_prefs
        return clean_prefs(value)
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
    if field == "timezone":
        name = str(value or "").strip()
        if not name:
            return ""
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(name)
        except Exception:
            raise ValueError("Unknown timezone.") from None
        return name
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

    @staticmethod
    def _principal(identity):
        # Plain IDs remain the Telegram compatibility entry point. Web adapters
        # must pass a Principal created only after session validation.
        return identity if isinstance(identity, Principal) else Principal.from_telegram(identity)

    def ensure_indexes(self):
        # Web-owned docs have no telegram_id, so this unique index must be sparse.
        # Drop an older non-sparse version once, if present.
        for index in list(self.col.list_indexes()):
            if (dict(index.get("key", {})) == {"telegram_id": 1}
                    and index.get("unique") and not index.get("sparse")):
                self.col.drop_index(index["name"])
        self.col.create_index("telegram_id", unique=True, sparse=True)
        self.col.create_index("owner_id", unique=True, sparse=True)

    def _find_doc(self, principal):
        return self.col.find_one(owner_filter(principal))

    def get(self, telegram_id) -> dict:
        principal = self._principal(telegram_id)
        doc = self._find_doc(principal) or {}
        out = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
        for k in DEFAULTS:
            if k in doc:
                out[k] = doc[k]
        return out

    def update(self, telegram_id, **fields) -> dict:
        principal = self._principal(telegram_id)
        clean = {k: _clean(k, v) for k, v in fields.items()}
        existing = self._find_doc(principal)
        values = {**clean, "owner_id": principal.owner_id, "updated_at": self.clock()}
        if existing:
            # Updating a legacy Telegram document also stamps its owner_id.
            self.col.update_one({"_id": existing["_id"]}, {"$set": values})
        else:
            insert_values = {"owner_id": principal.owner_id}
            if principal.kind == "telegram":
                insert_values["telegram_id"] = principal.subject
            self.col.update_one(
                {"owner_id": principal.owner_id},
                {"$set": {**clean, "updated_at": self.clock()},
                 "$setOnInsert": insert_values},
                upsert=True)
        return self.get(principal)

    def set_web_autopilot(self, principal, on: bool) -> dict:
        """Browser switch: ON = autofill + full auto (photos in, posts out)."""
        if not isinstance(principal, Principal) or principal.kind != "web":
            raise ValueError("A verified web principal is required.")
        if on:
            return self.update(principal, enabled=True, full_auto=True, paused=False)
        return self.update(principal, enabled=False, full_auto=False)

    def set_paused(self, telegram_id, paused: bool) -> dict:
        return self.update(telegram_id, paused=paused)

    # Posting autopilot and comment engagement are independent opt-ins.
    # A user who enabled AI replies should not have to enable auto-scheduling.
    _ENABLED_QUERY = {"$or": [
        {"enabled": True},
        {"auto_replies": True},
        {"auto_comments": True},
    ]}

    def enabled_users(self):
        """Telegram IDs only (legacy entry point; web owners are never returned)."""
        return [d["telegram_id"] for d in self.col.find(self._ENABLED_QUERY)
                if d.get("telegram_id")]

    def enabled_principals(self, include_web=False):
        """Verified owners with any autopilot feature on.

        Telegram owners always; web owners only when include_web is True (the
        worker passes True once it can run web-owned jobs). Documents whose
        owner/identity fields disagree are skipped (fail closed), and each
        owner appears once.
        """
        out, seen = [], set()
        for d in self.col.find(self._ENABLED_QUERY):
            principal = document_principal(d)
            if principal is None or (principal.kind == "web" and not include_web):
                continue
            if principal.owner_id in seen:
                continue
            seen.add(principal.owner_id)
            out.append(principal)
        return out


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
    "/autopilot vibe desi — caption vibe: auto | aesthetic | desi | funny | minimal\n"
    "/autopilot replies auto | off — AI replies to comments on your own posts\n"
    "/autopilot comments auto | off — AI posts a starter comment on new posts\n"
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
        f"• vibe: {s['vibe']}\n"
        f"• AI comment replies: {'on' if s.get('auto_replies') else 'off'}\n"
        f"• AI starter comments: {'on' if s.get('auto_comments') else 'off'}\n"
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
        if cmd == "vibe" and len(rest) == 1:
            s = store.update(telegram_id, vibe=rest[0])
            preset = VIBE_PRESETS.get(s["vibe"])
            return ("🎨 Vibe: auto — captions match each photo."
                    if s["vibe"] == "auto" else
                    f"🎨 Vibe: {s['vibe']} — {preset['blurb']}")
        if cmd == "trends" and len(rest) == 1 and rest[0].lower() in ("auto", "off"):
            s = store.update(telegram_id, trend_stories=rest[0].lower())
            if s["trend_stories"] == "auto":
                return ("📰 Trend Stories: auto. At most one a day, only entertainment/music/"
                        "sports/fashion/tech/festival/lifestyle topics with 2+ news sources, "
                        "never politics, courts, crime, deaths or your blocked topics. "
                        "Needs /autopilot on and the story format. /pause stops everything.")
            return "⚪ Trend Stories off."
        if cmd in ("replies", "comments") and len(rest) == 1 and rest[0].lower() in ("auto", "off"):
            field = "auto_replies" if cmd == "replies" else "auto_comments"
            enabled = rest[0].lower() == "auto"
            store.update(telegram_id, **{field: enabled})
            label = "AI comment replies" if cmd == "replies" else "AI starter comments"
            return f"{'🟢' if enabled else '⚪'} {label} {'enabled' if enabled else 'disabled'}."
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
