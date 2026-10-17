"""Occasion catalog: metadata + dates that roll forward every year.

Three date sources are merged:
  1. built-in fixed-date rules (Valentine's, Independence Day, Mother's Day...)
     computed for ANY year, so they never expire;
  2. event_calendar.json (lunar festivals such as Diwali/Holi/Eid) - these
     need a yearly refresh, see calendar_health();
  3. the user's personal dates (birthday, anniversary...), recurring yearly.
"""
from __future__ import annotations

import calendar as _cal
import re
from dataclasses import dataclass
from datetime import date, timedelta

TONES = ("celebratory", "warm", "fun", "reflective", "solemn")
CATEGORIES = ("festival", "national", "social", "seasonal", "awareness", "personal")
DEFAULT_CATEGORIES = ("festival", "national", "social", "seasonal", "personal")

# (category, tone, peak hours "HH:MM", aliases)
META = {
    "diwali": ("festival", "celebratory", ("18:30", "19:30", "20:30"), ("deepavali", "diwali_lights", "diya", "diyas", "lamps")),
    "chhoti_diwali": ("festival", "celebratory", ("18:30", "19:30"), ("naraka_chaturdashi", "choti_diwali")),
    "dhanteras": ("festival", "warm", ("11:00", "18:30"), ("dhanatrayodashi",)),
    "govardhan_puja": ("festival", "warm", ("10:00", "18:00"), ("annakut",)),
    "bhai_dooj": ("festival", "warm", ("11:00", "17:00"), ("bhaiya_dooj", "bhau_beej")),
    "dussehra": ("festival", "celebratory", ("17:30", "19:00"), ("vijayadashami", "dasara", "dashain")),
    "karwa_chauth": ("festival", "warm", ("19:00", "20:30"), ("karva_chauth",)),
    "chhath_puja": ("festival", "warm", ("06:00", "17:00"), ("chhath",)),
    "holi": ("festival", "fun", ("09:30", "11:30", "17:00"), ("festival_of_colours", "festival_of_colors", "rangwali_holi", "colours", "colors")),
    "holika_dahan": ("festival", "warm", ("18:30", "19:30"), ("choti_holi",)),
    "maha_shivratri": ("festival", "reflective", ("07:30", "19:00"), ("mahashivratri", "shivratri")),
    "eid_ul_fitr": ("festival", "celebratory", ("10:00", "18:30"), ("eid", "eid_mubarak", "id_ul_fitr")),
    "eid_ul_adha": ("festival", "celebratory", ("10:00", "18:30"), ("eid", "bakrid", "bakra_eid")),
    "ram_navami": ("festival", "warm", ("08:30", "18:30"), ("ramnavami",)),
    "chaitra_navratri": ("festival", "warm", ("08:30", "19:00"), ("navratri",)),
    "baisakhi": ("festival", "celebratory", ("10:00", "17:30"), ("vaisakhi",)),
    "makar_sankranti": ("festival", "fun", ("10:00", "16:30"), ("sankranti", "uttarayan", "patang", "kite", "kites")),
    "lohri": ("festival", "fun", ("18:30", "20:00"), ()),
    "pongal": ("festival", "warm", ("09:00", "17:00"), ("thai_pongal",)),
    "vasant_panchami": ("festival", "warm", ("08:30", "17:00"), ("basant_panchami", "saraswati_puja")),
    "buddha_purnima": ("festival", "reflective", ("08:00", "18:30"), ("vesak", "buddha_jayanti")),
    "mahavir_jayanti": ("festival", "reflective", ("08:30",), ()),
    "gudi_padwa": ("festival", "warm", ("09:00", "18:00"), ("ugadi", "chaitra_shukladi")),
    "ugadi": ("festival", "warm", ("09:00", "18:00"), ("gudi_padwa",)),
    "tamil_new_year": ("festival", "warm", ("09:00", "18:00"), ("puthandu",)),
    "vishu": ("festival", "warm", ("07:30", "18:00"), ()),
    "ambedkar_jayanti": ("national", "reflective", ("09:00",), ("ambedkar",)),
    "islamic_new_year": ("festival", "reflective", ("09:00",), ("muharram_new_year", "hijri_new_year")),
    "ashura": ("festival", "solemn", ("09:00",), ("muharram",)),
    "good_friday": ("festival", "solemn", ("09:00",), ()),
    "easter_sunday": ("festival", "warm", ("09:30", "17:00"), ("easter",)),
    "christmas": ("festival", "celebratory", ("09:00", "18:30", "20:00"), ("xmas", "christmas_day", "christmas_tree", "santa")),
    "christmas_eve": ("festival", "warm", ("18:00", "20:30"), ("xmas_eve",)),
    "new_year": ("social", "celebratory", ("08:30", "10:00", "19:30"), ("new_years_day", "happy_new_year", "newyear")),
    "new_years_eve": ("social", "celebratory", ("20:30", "22:30"), ("nye", "new_year_eve", "countdown")),
    "lunar_new_year": ("festival", "celebratory", ("10:00", "19:00"), ("chinese_new_year", "cny")),
    "nowruz": ("festival", "warm", ("10:00", "18:00"), ("navroz",)),
    "valentines_day": ("social", "warm", ("12:30", "19:30", "21:00"), ("valentine", "valentines", "valentine_day", "date_night")),
    "halloween": ("social", "fun", ("18:30", "20:30"), ("spooky", "pumpkin")),
    "st_patricks_day": ("social", "fun", ("17:00",), ("st_paddys_day",)),
    "april_fools_day": ("social", "fun", ("10:00", "14:00"), ()),
    "mothers_day": ("social", "warm", ("09:00", "12:30", "19:30"), ("mother_day", "mom", "mum", "maa")),
    "fathers_day": ("social", "warm", ("09:00", "12:30", "19:30"), ("father_day", "dad", "papa")),
    "friendship_day": ("social", "fun", ("12:30", "19:30"), ("friends", "friendship", "bestie", "besties", "squad")),
    "international_womens_day": ("social", "warm", ("09:00", "12:30"), ("womens_day", "women_day", "iwd")),
    "international_mens_day": ("social", "warm", ("12:30",), ("mens_day",)),
    "childrens_day_india": ("social", "warm", ("12:30",), ("childrens_day", "bal_diwas")),
    "teachers_day": ("social", "warm", ("09:00", "12:30"), ("teacher_day",)),
    "world_music_day": ("social", "fun", ("12:30", "19:30"), ("fete_de_la_musique", "music")),
    "international_yoga_day": ("social", "reflective", ("06:30", "09:00"), ("yoga_day", "yoga")),
    "earth_day": ("awareness", "reflective", ("10:00",), ()),
    "world_environment_day": ("awareness", "reflective", ("10:00",), ("environment_day",)),
    "world_photography_day": ("social", "fun", ("12:30", "19:30"), ("photography_day", "photographers_day")),
    "international_youth_day": ("social", "fun", ("12:30", "19:30"), ("youth_day",)),
    "world_smile_day": ("social", "fun", ("12:30",), ("smile_day",)),
    "labour_day": ("social", "reflective", ("10:00",), ("may_day", "workers_day")),
    "republic_day": ("national", "celebratory", ("09:00", "12:30"), ("26_january", "26_jan")),
    "independence_day": ("national", "celebratory", ("09:00", "12:30"), ("15_august", "15_aug", "swatantrata_diwas")),
    "gandhi_jayanti": ("national", "reflective", ("09:00",), ("gandhi_birthday", "gandhi")),
    "constitution_day": ("national", "reflective", ("10:00",), ("samvidhan_diwas",)),
    "national_youth_day": ("national", "warm", ("10:00",), ("vivekananda_jayanti",)),
    "vijay_diwas": ("national", "reflective", ("10:00",), ()),
    "martyrs_day": ("national", "solemn", ("11:00",), ("shaheed_diwas_gandhi", "sarvodaya_diwas")),
    "shaheed_diwas": ("national", "solemn", ("10:00",), ("shaheed_diwas_bhagat",)),
    "jallianwala_bagh_remembrance": ("national", "solemn", ("10:00",), ("jallianwala_bagh",)),
    "holocaust_remembrance_day": ("awareness", "solemn", ("10:00",), ()),
}

# Anything not in META that smells serious is treated as solemn: never a party tone.
_SOLEMN_WORDS = ("cancer", "aids", "tb_day", "malaria", "abuse", "violence", "terror", "remembrance",
                 "martyr", "shaheed", "holocaust", "lupus", "haemophilia", "pneumonia", "tsunami",
                 "diabetes", "obesity", "refugee", "migrants", "drug", "tobacco", "slavery",
                 "child_labour", "fgm", "rare_disease", "thalassaemia", "mine_awareness", "blood_donor",
                 "autism", "down_syndrome", "disability", "hearing", "aggression", "elder")

# (month, day) or ("nth", month, weekday 0=Mon, n) ; n=-1 for last
FIXED = {
    "new_year": ((1, 1), 85, 1, 1), "valentines_day": ((2, 14), 85, 7, 0),
    "international_womens_day": ((3, 8), 85, 3, 0), "earth_day": ((4, 22), 70, 2, 0),
    "labour_day": ((5, 1), 65, 0, 0), "mothers_day": (("nth", 5, 6, 2), 85, 5, 0),
    "fathers_day": (("nth", 6, 6, 3), 85, 5, 0), "world_environment_day": ((6, 5), 70, 2, 0),
    "international_yoga_day": ((6, 21), 60, 1, 0), "world_music_day": ((6, 21), 65, 1, 0),
    "independence_day": ((8, 15), 92, 2, 0), "friendship_day": (("nth", 8, 6, 1), 80, 3, 0),
    "international_youth_day": ((8, 12), 60, 1, 0), "world_photography_day": ((8, 19), 70, 1, 0),
    "teachers_day": ((9, 5), 75, 2, 0), "gandhi_jayanti": ((10, 2), 70, 0, 0),
    "world_smile_day": (("nth", 10, 4, 1), 50, 0, 0), "halloween": ((10, 31), 75, 3, 0),
    "childrens_day_india": ((11, 14), 80, 1, 0), "international_mens_day": ((11, 19), 60, 0, 0),
    "constitution_day": ((11, 26), 70, 0, 0), "christmas_eve": ((12, 24), 75, 2, 0),
    "christmas": ((12, 25), 85, 2, 0), "new_years_eve": ((12, 31), 85, 3, 0),
    "republic_day": ((1, 26), 90, 1, 0), "national_youth_day": ((1, 12), 65, 0, 0),
    "martyrs_day": ((1, 30), 70, 0, 0), "st_patricks_day": ((3, 17), 50, 0, 0),
    "april_fools_day": ((4, 1), 65, 0, 0), "ambedkar_jayanti": ((4, 14), 70, 0, 0),
}


@dataclass(frozen=True)
class Occasion:
    key: str
    name: str
    date: date
    category: str
    tone: str
    priority: int
    lead: int            # days before the date the occasion "opens"
    after: int           # days after the date it stays relevant
    peak_hours: tuple
    aliases: tuple
    source: str          # builtin | calendar | personal

    @property
    def start(self):
        return self.date - timedelta(days=self.lead)

    @property
    def end(self):
        return self.date + timedelta(days=self.after)


def norm(text) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")


def _nth_weekday(year, month, weekday, n):
    if n == -1:
        last = _cal.monthrange(year, month)[1]
        d = date(year, month, last)
        return d - timedelta(days=(d.weekday() - weekday) % 7)
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _rule_date(rule, year):
    if rule[0] == "nth":
        return _nth_weekday(year, rule[1], rule[2], rule[3])
    return date(year, rule[0], rule[1])


def _title(key):
    return key.replace("_", " ").title()


def meta_for(key):
    """-> (category, tone, peak_hours, aliases) for any key, with safe defaults."""
    key = norm(key)
    if key in META:
        return META[key]
    solemn = any(w in key for w in _SOLEMN_WORDS)
    return ("awareness", "solemn" if solemn else "reflective", ("10:00", "12:30"), ())


def _make(key, name, day, priority, lead, after, source):
    cat, tone, peaks, aliases = meta_for(key)
    return Occasion(key, name or _title(key), day, cat, tone, int(priority),
                    int(lead), int(after), tuple(peaks), tuple(aliases), source)


def build_occasions(start, end, *, calendar_events=(), personal=()):
    """All occasions whose [start-lead, end+after] window touches [start, end]."""
    found = {}

    def add(occ):
        if occ.end < start or occ.start > end:
            return
        found[(occ.key, occ.date)] = occ     # later sources override earlier ones

    for year in range(start.year - 1, end.year + 2):
        for key, (rule, prio, lead, after) in FIXED.items():
            try:
                add(_make(key, None, _rule_date(rule, year), prio, lead, after, "builtin"))
            except ValueError:
                continue
    for ev in calendar_events:
        add(_make(ev["key"], ev.get("name"), ev["date"], ev.get("priority", 60),
                  ev.get("window_before_days", 1), ev.get("window_after_days", 0), "calendar"))
    for p in personal:
        try:
            month, dom = int(p["month"]), int(p["day"])
            for year in range(start.year - 1, end.year + 2):
                try:
                    d = date(year, month, dom)
                except ValueError:       # 29 Feb in a non-leap year
                    d = date(year, 2, 28)
                tone = p.get("tone") if p.get("tone") in TONES else "celebratory"
                key = "personal_" + norm(p["name"])
                add(Occasion(key, str(p["name"])[:60], d, "personal", tone,
                             int(p.get("priority", 95)), int(p.get("lead", 2)),
                             int(p.get("after", 1)), ("12:30", "19:30"), (), "personal"))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(found.values(), key=lambda o: (o.date, -o.priority))


def calendar_health(calendar_events, today=None):
    """Days of lunar-festival coverage left in event_calendar.json (0 = expired)."""
    today = today or date.today()
    dates = [e["date"] for e in calendar_events]
    if not dates:
        return {"covered_until": None, "days_left": 0, "ok": False}
    last = max(dates)
    left = (last - today).days
    return {"covered_until": last.isoformat(), "days_left": max(0, left), "ok": left >= 45}
