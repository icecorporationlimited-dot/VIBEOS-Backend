"""
publishing/policy.py — the rules every post must pass BEFORE it goes out.

PolicyEngine.check(job) returns a Decision:

  allow   publish now
  skip    leave the job untouched (autopilot paused / not due yet)
  defer   try again later (quiet hours, daily limit, min gap) — until=<when>
  hold    needs a human (blocked topic, format not allowed, duplicate content)

PublishService calls it before claiming a job and again right before
media_publish, so a /pause that lands mid-run still stops the post.

Manual jobs (user tapped Post) skip pause, quiet hours and limits — that is an
explicit request — but still obey blocked topics, formats and duplicate checks.
"""
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import service as svc
from .identity import Principal, document_principal

ALLOW, SKIP, DEFER, HOLD = "allow", "skip", "defer", "hold"
DEFAULT_TZ = "Asia/Kolkata"


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str = ""
    until: Optional[datetime] = None


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def get_zone(name):
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def resolve_tz(tz_for, who):
    """Timezone name for one owner.

    Web principals are handed to tz_for whole (their zone lives in their
    autopilot settings). Telegram owners keep the legacy call with the plain
    numeric ID, so existing tz_for callables keep working unchanged.
    """
    if isinstance(who, Principal):
        return tz_for(who) if who.kind == "web" else tz_for(who.subject)
    return tz_for(who)


def make_tz_for(autopilot, telegram_tz):
    """Build the timezone lookup shared by the policy gate, calendar and planner.

    telegram_tz(telegram_id) -> IANA name is the legacy per-Telegram-user lookup.
    Web owners keep their zone in their autopilot settings ("" = default).
    """
    def tz_for(who):
        if isinstance(who, Principal):
            if who.kind == "web":
                return autopilot.get(who).get("timezone") or DEFAULT_TZ
            who = who.subject
        return telegram_tz(who)
    return tz_for


def in_quiet(local_dt, start, end) -> bool:
    """Is this local time inside quiet hours? start==end means no quiet hours."""
    h = local_dt.hour + local_dt.minute / 60
    if start == end:
        return False
    return start <= h < end if start < end else (h >= start or h < end)


def quiet_end_at(local_dt, end):
    """Next local moment the quiet window ends."""
    cand = local_dt.replace(hour=end, minute=0, second=0, microsecond=0)
    return cand if cand > local_dt else cand + timedelta(days=1)


def day_bounds(local_dt):
    """[start, end) of the local calendar day, as UTC datetimes."""
    start = datetime.combine(local_dt.date(), time(0, 0), tzinfo=local_dt.tzinfo)
    end = datetime.combine(local_dt.date() + timedelta(days=1), time(0, 0),
                           tzinfo=local_dt.tzinfo)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


class PolicyEngine:
    def __init__(self, jobs, buffer, autopilot, tz_for, clock=_now):
        self.jobs, self.buffer, self.autopilot = jobs, buffer, autopilot
        self.tz_for, self.clock = tz_for, clock

    def __call__(self, job, final=False):      # PublishService.gate
        return self.check(job, final=final)

    # ------------------------------------------------------------------
    def check(self, job, final=False) -> Decision:
        now = self.clock()
        principal = document_principal(job)
        if principal is None:                      # never decide for an unknown owner
            return Decision(SKIP, "unknown owner")
        s = self.autopilot.get(principal)
        manual = job.get("source", "manual") == "manual"

        if not final and job.get("next_attempt_at") and _aware(job["next_attempt_at"]) > now:
            return Decision(SKIP, "not due yet")
        if s["paused"] and not manual:
            return Decision(SKIP, "autopilot paused")

        if not job.get("policy_override"):
            if job["format"] not in s["formats"]:
                return Decision(HOLD, f"{job['format']} posts are not allowed by your autopilot settings")
            topic = self._blocked_topic(job, s["blocked_topics"])
            if topic:
                return Decision(HOLD, f"contains blocked topic “{topic}”")

        dup = self._duplicate_of(job)
        if dup:
            return Decision(HOLD, f"same content was already posted (job {dup['_id'][:6]})")

        if manual:
            return Decision(ALLOW)

        tz = get_zone(resolve_tz(self.tz_for, principal))
        local = now.astimezone(tz)
        if in_quiet(local, s["quiet_start"], s["quiet_end"]):
            return Decision(DEFER, "quiet hours",
                            quiet_end_at(local, s["quiet_end"]).astimezone(timezone.utc))

        published = self._published_times(job)
        start, end = day_bounds(local)
        if len([t for t in published if start <= t < end]) >= s["daily_limit"]:
            return Decision(DEFER, "daily limit reached", end)
        if published and s["min_gap_minutes"]:
            ready_at = max(published) + timedelta(minutes=s["min_gap_minutes"])
            if ready_at > now:
                return Decision(DEFER, "minimum gap between posts", ready_at)
        return Decision(ALLOW)

    # ------------------------------------------------------------------
    def _blocked_topic(self, job, topics):
        if not topics:
            return None
        from story_engine import safety              # lazy: keeps publishing importable without story_engine deps
        parts = []
        for aid in job["asset_ids"]:
            asset = self.buffer.assets.find_one({"_id": aid}) or {}
            creative = asset.get("creative") or {}
            if job["format"] != "story":   # stories are posted without caption
                parts.append(svc.build_caption(creative))
            parts.append(" ".join(f"#{str(t).lstrip('#')}" for t in creative.get("hashtags") or []))
            if job["format"] == "story":
                # A trend Story has no caption/hashtags: what the reader sees is the card text, so check the
                # trend headline, the card copy and the publishers' headlines that queue_story stored with it.
                dec = (asset.get("route") or {}).get("decision") or {}
                parts.append((asset.get("analysis") or {}).get("trend_title") or "")
                parts.append(dec.get("trend_title") or "")
                parts.append(" ".join(str(x) for x in dec.get("copy") or []))
                parts.append(" ".join(str(x) for x in dec.get("source_titles") or []))
        return safety.topic_hit(topics, *parts)

    def _duplicate_of(self, job):
        digest = job.get("content_hash")
        if not digest:
            return None
        live = (svc.PUBLISHED, svc.NEEDS_ATTENTION, svc.RUNNING)
        for other in self.jobs.find({"ig_user_id": job["ig_user_id"], "content_hash": digest}):
            if other["_id"] != job["_id"] and other["format"] == job["format"] \
                    and other["status"] in live:
                return other
        return None

    def _published_times(self, job):
        return [_aware(j["published_at"])
                for j in self.jobs.find({"ig_user_id": job["ig_user_id"],
                                         "status": svc.PUBLISHED})
                if j.get("published_at") and j["_id"] != job["_id"]]
