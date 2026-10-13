"""Stage 5: public and school holiday flags per date. Pure functions (the CSV is read once, on first use).

Public holidays come from the `holidays` package: Singapore, and Malaysia subdivision "01" = Johor. School holidays
come from data/school_holidays.csv, filled in by hand (columns country,start,end,name; dates YYYY-MM-DD).
"""
import csv
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import holidays

SCHOOL_CSV = Path(__file__).parent / "data" / "school_holidays.csv"
SG = holidays.country_holidays("SG")
JOHOR = holidays.country_holidays("MY", subdiv="01")
DAY = timedelta(days=1)


def load_school_holidays(path=SCHOOL_CSV):
    """Rows of the school holiday CSV, with dates parsed. A missing file means no school holidays yet."""
    path = Path(path)
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [{"country": r["country"].strip(), "start": date.fromisoformat(r["start"].strip()),
                 "end": date.fromisoformat(r["end"].strip()), "name": r["name"].strip()}
                for r in csv.DictReader(f) if r.get("start")]


@lru_cache(maxsize=1)
def _school():
    return tuple((r["start"], r["end"], r["name"]) for r in load_school_holidays())


def _as_date(d):
    return d if isinstance(d, date) else date.fromisoformat(str(d)[:10])


def is_ph(d):
    """A public holiday in Singapore or Johor."""
    return d in SG or d in JOHOR


def is_off(d):
    """A day most people don't work: Saturday, Sunday (the weekend in both Singapore and Johor) or a public holiday."""
    return d.weekday() >= 5 or is_ph(d)


def day_flags(d, school=None):
    """Flags for one date. `school` = rows from load_school_holidays (default: the project CSV)."""
    d = _as_date(d)
    ranges = _school() if school is None else tuple((r["start"], r["end"], r["name"]) for r in school)
    # last day of a long weekend: a day off followed by a working day, in a run of days off that includes a holiday
    last_day = False
    if is_off(d) and not is_off(d + DAY):
        x = d
        while is_off(x):
            last_day = last_day or is_ph(x)
            x -= DAY
    school_names = [name for start, end, name in ranges if start <= d <= end]
    return {
        "sg_ph": d in SG,
        "my_ph": d in JOHOR,
        "ph_eve": not is_ph(d) and is_ph(d + DAY),
        "ph_last_day": last_day,
        "school_hol": bool(school_names),
        "names": [n for n in (SG.get(d), JOHOR.get(d)) if n] + school_names,
    }


def day_type(d, school=None):
    """'holiday' (public holiday in Singapore or Johor), 'weekend' or 'weekday'. School holidays don't change it."""
    f = day_flags(d, school)
    if f["sg_ph"] or f["my_ph"]:
        return "holiday"
    return "weekend" if _as_date(d).weekday() >= 5 else "weekday"


def is_special(d, school=None):
    """A public holiday, its eve, or the last day of a long weekend: kept out of the 'typical' figures."""
    f = day_flags(d, school)
    return f["sg_ph"] or f["my_ph"] or f["ph_eve"] or f["ph_last_day"]
