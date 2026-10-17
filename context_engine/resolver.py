"""Map an AI-detected label ("durga puja", "eid", "bday") to a catalog Occasion."""
from __future__ import annotations

import difflib
from datetime import date

from .catalog import Occasion, norm

_BDAY = {"birthday", "bday", "birthday_party", "happy_birthday", "cake_cutting"}


def _names(o: Occasion):
    return (o.key,) + tuple(norm(a) for a in o.aliases)


def resolve(label, occasions, today: date | None = None):
    """Best Occasion for `label`, or None. Ongoing/upcoming beats past."""
    today = today or date.today()
    k = norm(label)
    if not k or not occasions:
        return None
    if k in _BDAY:
        pool = [o for o in occasions if o.category == "personal" and "birthday" in o.key]
        return _nearest(pool, today)
    cands = [o for o in occasions if k in _names(o)]
    if not cands and len(k) >= 4:
        kt = set(k.split("_"))
        for o in occasions:
            for n in _names(o):
                nt = set(n.split("_"))
                if len(nt) > 1 and (nt <= kt or kt <= nt) and len(kt) > 1:
                    cands.append(o)
                    break
    if not cands and len(k) >= 5:
        universe = {n: o for o in occasions for n in _names(o)}
        close = difflib.get_close_matches(k, list(universe), n=3, cutoff=0.84)
        cands = [o for o in occasions if any(n in close for n in _names(o))]
    return _nearest(cands, today)


def _nearest(cands, today):
    if not cands:
        return None
    live = [o for o in cands if o.end >= today]
    if live:
        return min(live, key=lambda o: (abs((o.date - today).days), -o.priority))
    return max(cands, key=lambda o: o.date)
