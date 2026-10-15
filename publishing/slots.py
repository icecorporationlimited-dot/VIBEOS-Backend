"""
publishing/slots.py — the content calendar: "which time should the next post get?"

Simple, predictable rules (Phase 5 analytics can later replace the slot list):
  * candidates are the user's preferred local `slots` (default 12:30 and 19:30)
  * never inside quiet hours
  * never on a day that already has `daily_limit` posts (published + scheduled)
  * never closer than `min_gap_minutes` to another post
"""
from datetime import datetime, time, timedelta, timezone

from . import service as svc
from .policy import _aware, day_bounds, get_zone, in_quiet


def _now():
    return datetime.now(timezone.utc)


class Calendar:
    def __init__(self, jobs, autopilot, tz_for, clock=_now):
        self.jobs, self.autopilot, self.tz_for, self.clock = jobs, autopilot, tz_for, clock

    def _busy_times(self, telegram_id):
        times = []
        for j in self.jobs.find({"telegram_id": str(telegram_id)}):
            if j["status"] == svc.PUBLISHED and j.get("published_at"):
                times.append(_aware(j["published_at"]))
            elif j["status"] in (svc.QUEUED, svc.RETRY, svc.RUNNING):
                when = j.get("scheduled_at") or j.get("next_attempt_at")
                if when:
                    times.append(_aware(when))
        return times

    def next_slot(self, telegram_id, horizon_days=7, not_before=None, not_after=None):
        """Next free slot as UTC datetime.

        Optional aware local datetimes constrain the candidate window; normal
        callers can omit them and retain the original calendar behaviour.
        """
        s = self.autopilot.get(telegram_id)
        tz = get_zone(self.tz_for(telegram_id))
        now = self.clock()
        local_now = now.astimezone(tz)
        if not_before is not None:
            if not_before.tzinfo is None:
                not_before = not_before.replace(tzinfo=tz)
            local_now = max(local_now, not_before.astimezone(tz))
        if not_after is not None:
            if not_after.tzinfo is None:
                not_after = not_after.replace(tzinfo=tz)
            not_after = not_after.astimezone(tz)
        gap = timedelta(minutes=s["min_gap_minutes"])
        busy = self._busy_times(telegram_id)

        for offset in range(horizon_days + 1):
            day = local_now.date() + timedelta(days=offset)
            day_local = datetime.combine(day, time(12, 0), tzinfo=tz)
            start, end = day_bounds(day_local)
            if len([t for t in busy if start <= t < end]) >= s["daily_limit"]:
                continue
            for slot in s["slots"]:
                hh, mm = (int(x) for x in slot.split(":"))
                local = datetime.combine(day, time(hh, mm), tzinfo=tz)
                if not_after is not None and local > not_after:
                    continue
                cand = local.astimezone(timezone.utc)
                if cand <= now + timedelta(minutes=1):
                    continue
                if in_quiet(local, s["quiet_start"], s["quiet_end"]):
                    continue
                if any(abs(cand - t) < gap for t in busy):
                    continue
                return cand
        return None
