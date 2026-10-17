"""ContextPlanner: drop-in upgrade of ContentPlanner.

What it adds on top of the old exact-key lookup:
  * alias/fuzzy matching and yearly-rolling dates (catalog + resolver);
  * per-user preferences (categories, minimum priority, solemn handling, personal dates);
  * arc planning: the occasion day first, then the eve, afterglow and teaser days;
  * occasion peak-hour slots (Diwali evening, Holi morning...) and a time-of-day
    fit for ordinary photos (sunset photo -> evening slot);
  * occasion gap-filling: when a big occasion is near and the user uploaded no
    matching photo, the best-fitting ready photo becomes a greeting post;
  * an occasion context string that the caption writer can use.
It keeps the (slot, meta) contract of ContentPlanner.schedule_for, so the
Worker needs no change for the scheduling part.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, time, timedelta, timezone

from content_planner import ContentPlanner, _parse_visible_date, _principal
from event_calendar import DEFAULT_PATH, load_events
from publishing.policy import get_zone, resolve_tz

from .catalog import build_occasions, calendar_health
from .prefs import DEFAULT_PREFS, clean_prefs  # noqa: F401  (re-exported)
from .resolver import resolve

logger = logging.getLogger(__name__)

LOOKAHEAD_DAYS = 75
FILL_MIN_FIT = 0.5

_MORNING = re.compile(r"sunrise|morning|breakfast|coffee|brunch|yoga|dawn|cafe|bakery")
_EVENING = re.compile(r"sunset|golden hour|dusk|evening|dinner|night|party|club|neon|concert|fireworks|lights|lamp|diya")
_HAPPY = re.compile(r"smil|laugh|bright|colou?rful|friends|group|dance|party|outfit|festive|celebrat|joy|candid|cake")
_HEAVY = re.compile(r"sad|crying|dark|gloomy|hospital|funeral|angry|serious|alone in the dark")

TONE_BRIEF = {
    "celebratory": "Joyful and festive. Warm greeting, light emoji (max 2).",
    "warm": "Warm, personal, heartfelt. Gratitude or family feeling; no party slang.",
    "fun": "Playful and light, a little cheeky. Short punchy lines.",
    "reflective": "Calm and thoughtful. No hype, no party slang, at most 1 emoji.",
    "solemn": "Respectful and quiet. NO jokes, NO party tone, NO emoji except at most one candle or flower. Do not use the photo as a prop for the topic.",
}
PHASE_BRIEF = {
    "teaser": "It is a few days BEFORE the occasion: build anticipation, do not say it is today.",
    "eve": "It is the DAY BEFORE the occasion: excitement, countdown feeling.",
    "day": "It is the occasion DAY: greet directly.",
    "afterglow": "The occasion just ended: a grateful, memory-style post.",
}


def day_phase(occ, day):
    if day == occ.date:
        return "day"
    if day < occ.date:
        return "eve" if (occ.date - day).days == 1 else "teaser"
    return "afterglow"


def caption_context(occ, phase="day"):
    return (
        "Occasion context (authoritative, from the calendar; you MAY mention it even if the "
        "photo does not show it, and one hashtag may be the occasion):\n"
        f"- Occasion: {occ.name}\n- Tone: {occ.tone} - {TONE_BRIEF[occ.tone]}\n"
        f"- Timing: {PHASE_BRIEF[phase]}\n"
        + ("- This is the creator's OWN personal occasion; write in first person.\n"
           if occ.category == "personal" else
           "- Never claim religious or political beliefs for the creator beyond a respectful greeting.\n"))


class ContextPlanner(ContentPlanner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._clock = lambda: datetime.now(timezone.utc)

    # ---- preferences / catalog ----------------------------------------------
    def prefs(self, who) -> dict:
        principal = _principal(who)
        try:
            saved = (self.calendar.autopilot.get(principal) or {}).get("occasions") or {}
        except Exception:
            saved = {}
        out = {**DEFAULT_PREFS, **{k: v for k, v in saved.items() if k in DEFAULT_PREFS}}
        out["categories"] = list(out["categories"])
        return out

    def events(self):
        return load_events(self.event_calendar_path or DEFAULT_PATH)

    def occasions(self, who, today, *, days_ahead=LOOKAHEAD_DAYS, back=8, prefs=None):
        prefs = prefs or self.prefs(who)
        if not prefs["enabled"]:
            return []
        out = []
        for o in build_occasions(today - timedelta(days=back), today + timedelta(days=days_ahead),
                                 calendar_events=self.events(), personal=prefs["personal"]):
            if o.category != "personal":
                if o.category not in prefs["categories"] or o.priority < prefs["min_priority"]:
                    continue
            if o.tone == "solemn" and prefs["solemn"] == "skip":
                continue
            out.append(o)
        return out

    def health(self):
        return calendar_health(self.events())

    # ---- slots --------------------------------------------------------------
    def _local_today(self, who):
        tz = get_zone(resolve_tz(self.tz_for, _principal(who)))
        return tz, self._clock().astimezone(tz)

    def _phase_slot(self, who, occ):
        """Occasion day first, then eve, afterglow, teaser. -> (utc slot, local day) or (None, None)."""
        tz, now = self._local_today(who)
        order, span = [occ.date], max(occ.lead, occ.after, 1)
        for i in range(1, span + 1):
            if i <= occ.lead:
                order.append(occ.date - timedelta(days=i))
            if i <= occ.after:
                order.append(occ.date + timedelta(days=i))
        order.sort(key=lambda d: (abs((d - occ.date).days), d > occ.date))
        for day in order:
            if day < now.date() or not (occ.start <= day <= occ.end):
                continue
            slot = self.calendar.next_slot(
                who, horizon_days=1,
                not_before=datetime.combine(day, time(0, 0), tzinfo=tz),
                not_after=datetime.combine(day, time(23, 59), tzinfo=tz),
                prefer_slots=list(occ.peak_hours) or None)
            if slot is None:                              # peak hours taken: any normal slot that day
                slot = self.calendar.next_slot(
                    who, horizon_days=1,
                    not_before=datetime.combine(day, time(0, 0), tzinfo=tz),
                    not_after=datetime.combine(day, time(23, 59), tzinfo=tz))
            if slot is not None:
                return slot, day
        return None, None

    def daypart_slots(self, who, detection):
        """User slots that match the photo's light/mood, or None (= use all)."""
        text = str((detection or {}).get("visual_summary") or "").lower()
        if not text:
            return None
        want = "morning" if _MORNING.search(text) else "evening" if _EVENING.search(text) else None
        if not want:
            return None
        slots = (self.calendar.autopilot.get(_principal(who)) or {}).get("slots") or []
        pick = [s for s in slots if (int(s[:2]) < 11) == (want == "morning")
                and (want == "morning" or int(s[:2]) >= 16)]
        return pick or None

    # ---- main hook used by Worker._slot_for ---------------------------------
    def schedule_for(self, telegram_id, asset: dict):
        who = _principal(telegram_id)
        detection = self.classify_asset(who, asset)
        tz, now = self._local_today(who)
        today = now.date()

        raw_date = str(detection.get("visible_date_text") or "").strip()
        explicit, _ = _parse_visible_date(raw_date, today)
        if explicit:                                   # a printed date always wins
            if explicit < today:
                return None, {"event_status": "printed_date_passed", "event_date": explicit.isoformat(),
                              "visible_date_text": raw_date, "event_detection": detection}
            when = self.calendar.next_slot(
                who, horizon_days=1,
                not_before=datetime.combine(explicit, time(0, 0), tzinfo=tz),
                not_after=datetime.combine(explicit, time(23, 59), tzinfo=tz))
            status = "date_pinned" if when else "date_full"
            return when, {"event_status": status, "event_date": explicit.isoformat(),
                          "visible_date_text": raw_date, "event_detection": detection}

        prefs = self.prefs(who)
        general = {"event_status": "general_or_uncertain", "event_detection": detection}
        if (prefs["enabled"] and detection.get("event_related")
                and detection.get("confidence", 0) >= self.min_confidence):
            occ = resolve(detection.get("event_key"), self.occasions(who, today, prefs=prefs), today)
            if occ is None:
                general["event_status"] = "no_calendar_match"
            elif occ.end < today:
                general.update(event_status="event_window_passed", event=occ.key,
                               event_date=occ.date.isoformat())
            else:
                when, day = self._phase_slot(who, occ)
                meta = {"event": occ.key, "event_name": occ.name, "event_date": occ.date.isoformat(),
                        "event_detection": detection, "tone": occ.tone, "category": occ.category}
                if when is None:
                    return None, {**meta, "event_status": "window_full"}
                return when, {**meta, "event_status": "reserved",
                              "phase": day_phase(occ, day), "slot_day": day.isoformat()}

        part = self.daypart_slots(who, detection)      # ordinary photo: time-of-day fit
        if part:
            when = self.calendar.next_slot(who, prefer_slots=part)
            if when:
                return when, {**general, "event_status": "general_daypart"}
        return None, general

    # ---- gap filling ---------------------------------------------------------
    def fit_score(self, asset, detection, occ):
        score = 0.0
        try:
            score = float(asset.get("web_score")) / 100.0
        except (TypeError, ValueError):
            q = (asset.get("analysis") or {}).get("quality_score")
            score = float(q) if isinstance(q, (int, float)) else 0.55
        text = str((detection or {}).get("visual_summary") or "").lower()
        if occ.tone in ("celebratory", "fun", "warm"):
            score += 0.12 * min(2, len(_HAPPY.findall(text))) - 0.2 * bool(_HEAVY.search(text))
        return max(0.0, min(1.0, score))

    def occasion_fill(self, who, ready, taken):
        """One greeting plan for a near occasion that has no photo yet, or None.

        `taken` is a set of (event_key, "YYYY-MM-DD") already covered by jobs.
        """
        who = _principal(who)
        prefs = self.prefs(who)
        if not (prefs["enabled"] and prefs["auto_greeting"]):
            return None
        tz, now = self._local_today(who)
        today = now.date()
        pool = [a for a in ready if a.get("kind", "photo") == "photo"
                and not a.get("route") and not a.get("last_published_at")]
        if not pool:
            return None
        cluster = self.occasions(who, today, days_ahead=10, back=4, prefs=prefs)
        for occ in sorted(self.occasions(who, today, days_ahead=4, back=0, prefs=prefs),
                          key=lambda o: (-o.priority, o.date)):
            if occ.tone == "solemn" or occ.priority < max(60, prefs["min_priority"]):
                continue
            # Festival clusters (Dhanteras..Bhai Dooj): greet only the headliner, never five days in a row.
            if any(o.key != occ.key and abs((o.date - occ.date).days) <= 3
                   and o.priority >= occ.priority + 10 for o in cluster):
                continue
            if (occ.key, occ.date.isoformat()) in taken:
                continue
            reach = min(max(occ.lead, 1), 3)
            if not (occ.date - timedelta(days=reach) <= today <= occ.date):
                continue
            best, best_fit = None, 0.0
            for a in pool:
                try:
                    det = self.classify_asset(who, a)
                except Exception:
                    det = {}
                if det.get("event_related") and det.get("confidence", 0) >= self.min_confidence:
                    continue                        # belongs to its own occasion
                fit = self.fit_score(a, det, occ)
                if fit > best_fit:
                    best, best_fit = a, fit
            if best is None or best_fit < FILL_MIN_FIT:
                continue
            when, day = self._phase_slot(who, occ)
            if when is None:
                continue
            return {"asset": best, "occasion": occ, "phase": day_phase(occ, day), "slot": when,
                    "fit": round(best_fit, 2), "caption_context": caption_context(occ, day_phase(occ, day)),
                    "meta": {"event_status": "reserved", "event": occ.key, "event_name": occ.name,
                             "event_date": occ.date.isoformat(), "tone": occ.tone,
                             "phase": day_phase(occ, day), "slot_day": day.isoformat(),
                             "filled_by": "occasion_fill", "fit": round(best_fit, 2)}}
        return None

    def occasion_caption(self, who, asset, occ, phase):
        """Rewrite the stored caption for an occasion greeting. Soft-fails -> False."""
        try:
            import base64
            import copywriter
            from publishing.web_pipeline import _parse_json, _small_jpeg
            from groq import Groq

            creative = asset.get("creative") or {}
            vibe = creative.get("vibe") or "natural"
            mode = creative.get("caption_style") or (vibe if vibe in copywriter.CAPTION_STYLES else "natural")
            recent = [((a.get("creative") or {}).get("caption") or "")
                      for a in self.buffer.list_assets(_principal(who), "ready")[-8:]]
            prompt = copywriter.build_prompt(mode=mode, vibe=vibe, recent_captions=recent,
                                             trend_context=caption_context(occ, phase))
            b64 = base64.b64encode(_small_jpeg(self.buffer.get_media(asset["_id"]))).decode()
            resp = Groq(api_key=self.api_key).chat.completions.create(
                model=self.model, temperature=0.8,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]}])
            parsed = copywriter.parse_copy(_parse_json(resp.choices[0].message.content), mode=mode)
            self.buffer.assets.update_one(
                {"_id": asset["_id"]},
                {"$set": {"creative.caption": parsed["caption"], "creative.hashtags": parsed["hashtags"],
                          "creative.occasion": {"key": occ.key, "phase": phase}}})
            return True
        except Exception:
            logger.exception("Occasion caption failed for %s; keeping the original caption", asset.get("_id"))
            return False

    def occasion_hint(self, who, asset):
        """Caption context for a photo whose own content matches a live occasion (cached detection)."""
        who = _principal(who)
        det = self.classify_asset(who, asset)
        if not (det.get("event_related") and det.get("confidence", 0) >= self.min_confidence):
            return None
        _, now = self._local_today(who)
        occ = resolve(det.get("event_key"), self.occasions(who, now.date()), now.date())
        if occ is None or occ.end < now.date():
            return None
        day = min(max(now.date(), occ.start), occ.end)
        return caption_context(occ, day_phase(occ, day))

    # ---- read-only view for the dashboard -----------------------------------
    def overview(self, who, jobs=()):
        """Upcoming occasions + what the engine did/will do about each."""
        who = _principal(who)
        prefs = self.prefs(who)
        _, now = self._local_today(who)
        today = now.date()
        taken = {}
        for j in jobs:
            if j.get("event_key") and j.get("status") not in ("failed", "cancelled"):
                taken.setdefault((j["event_key"], str(j.get("event_date"))), []).append(j)
        rows = []
        for o in self.occasions(who, today, days_ahead=45, back=0, prefs=prefs):
            mine = taken.get((o.key, o.date.isoformat()), [])
            if mine:
                state = "planned"
            elif o.tone == "solemn":
                state = "quiet"
            elif prefs["auto_greeting"] and o.priority >= max(60, prefs["min_priority"]):
                state = "auto_greeting"
            else:
                state = "watching"
            rows.append({"key": o.key, "name": o.name, "date": o.date.isoformat(),
                         "days_left": (o.date - today).days, "category": o.category,
                         "tone": o.tone, "priority": o.priority, "state": state,
                         "planned": len(mine)})
        return {"prefs": prefs, "occasions": rows[:40], "calendar": self.health()}
