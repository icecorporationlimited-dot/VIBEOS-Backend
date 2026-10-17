"""
publishing/slots.py — the content calendar: "which time should the next post get?"

Simple, predictable rules (Phase 5 analytics can later replace the slot list):
  * candidates are the user's preferred local `slots` (default 12:30 and 19:30)
  * never inside quiet hours
  * never on a day that already has `daily_limit` posts (published + scheduled)
  * never closer than `min_gap_minutes` to another post
"""
import random
from datetime import datetime, time, timedelta, timezone

from . import service as svc
from .policy import _aware, day_bounds, get_zone, in_quiet, resolve_tz
from .identity import Principal, owner_filter


def _now():
    return datetime.now(timezone.utc)


class Calendar:
    def __init__(self, jobs, autopilot, tz_for, clock=_now):
        self.jobs, self.autopilot, self.tz_for, self.clock = jobs, autopilot, tz_for, clock

    def _busy_times(self, telegram_id):
        principal = (telegram_id if isinstance(telegram_id, Principal)
                     else Principal.from_telegram(telegram_id))
        times = []
        query = owner_filter(principal)
        if principal.kind == "telegram":
            query = {"$and": [{"telegram_id": principal.subject}, query]}
        for j in self.jobs.find(query):
            if j["status"] == svc.PUBLISHED and j.get("published_at"):
                times.append(_aware(j["published_at"]))
            elif j["status"] in (svc.QUEUED, svc.RETRY, svc.RUNNING):
                when = j.get("scheduled_at") or j.get("next_attempt_at")
                if when:
                    times.append(_aware(when))
        return times

    def next_slot(self, telegram_id, horizon_days=10, not_before=None, not_after=None,
                  prefer_slots=None):
        """Next free slot as UTC datetime.

        Optional aware local datetimes constrain the candidate window; normal
        callers can omit them and retain the original calendar behaviour.
        `prefer_slots` (list of "HH:MM") replaces the user's slot list for this
        call only (occasion peak hours / time-of-day fit). Quiet hours, the
        daily limit and the minimum gap still apply.
        """
        principal = (telegram_id if isinstance(telegram_id, Principal)
                     else Principal.from_telegram(telegram_id))
        s = self.autopilot.get(principal)
        tz = get_zone(resolve_tz(self.tz_for, principal))
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
        slot_list = list(prefer_slots) if prefer_slots else s["slots"]

        # Spread general content over the whole planning horizon instead of
        # always filling tomorrow's first slot. Constrained event/date windows
        # remain constrained by not_before/not_after.
        offsets = list(range(horizon_days + 1))
        random.shuffle(offsets)
        for offset in offsets:
            day = local_now.date() + timedelta(days=offset)
            day_local = datetime.combine(day, time(12, 0), tzinfo=tz)
            start, end = day_bounds(day_local)
            if len([t for t in busy if start <= t < end]) >= s["daily_limit"]:
                continue
            for slot in slot_list:
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

    def companion_slot(self, telegram_id, photo_slot):
        """Slot for a Story that accompanies a photo already scheduled at
        `photo_slot`: same local day, at least the minimum gap later, never in
        quiet hours, never over the daily limit. None = no room that day.
        """
        principal = (telegram_id if isinstance(telegram_id, Principal)
                     else Principal.from_telegram(telegram_id))
        s = self.autopilot.get(principal)
        tz = get_zone(resolve_tz(self.tz_for, principal))
        now = self.clock()
        base = _aware(photo_slot).astimezone(tz)
        gap = timedelta(minutes=max(int(s["min_gap_minutes"]), 60))
        busy = self._busy_times(telegram_id)
        start, end = day_bounds(base)
        if len([t for t in busy if start <= t < end]) >= s["daily_limit"]:
            return None
        times = sorted(tuple(int(x) for x in slot.split(":")) for slot in s["slots"])
        cands = [datetime.combine(base.date(), time(hh, mm), tzinfo=tz) for hh, mm in times]
        cands = [c for c in cands if c - base >= gap]
        cands.append(base + timedelta(hours=3))      # fallback when no later preset slot
        for local in cands:
            if local.date() != base.date():
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
