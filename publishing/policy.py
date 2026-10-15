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
        s = self.autopilot.get(job["telegram_id"])
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

        tz = get_zone(self.tz_for(job["telegram_id"]))
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
        parts = []
        for aid in job["asset_ids"]:
            asset = self.buffer.assets.find_one({"_id": aid}) or {}
            creative = asset.get("creative") or {}
            if job["format"] != "story":   # stories are posted without caption
                parts.append(svc.build_caption(creative))
            parts.append(" ".join(f"#{str(t).lstrip('#')}" for t in creative.get("hashtags") or []))
        text = " ".join(parts).lower()
        for topic in topics:
            if re.search(r"(?<!\w)" + re.escape(topic) + r"(?!\w)", text):
                return topic
        return None

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
