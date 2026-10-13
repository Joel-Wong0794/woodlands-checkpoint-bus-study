"""Tests for calendar_flags.py. Holiday dates come from the holidays package (v0.106), not from these tests.
Run from the project root: python -m pytest"""
from datetime import date

from calendar_flags import day_flags, day_type, is_special, load_school_holidays

SCHOOL = [{"country": "SG", "start": date(2026, 11, 21), "end": date(2026, 12, 31), "name": "test range (made up)"}]


def test_christmas_is_a_public_holiday_in_both():
    f = day_flags(date(2026, 12, 25), SCHOOL)
    assert f["sg_ph"] and f["my_ph"]
    assert day_type(date(2026, 12, 25), SCHOOL) == "holiday"


def test_ordinary_tuesday_and_saturday():
    assert day_type(date(2026, 10, 13), SCHOOL) == "weekday"
    assert day_type(date(2026, 10, 17), SCHOOL) == "weekend"
    assert not any(v for k, v in day_flags(date(2026, 10, 13), SCHOOL).items() if k != "names")


def test_school_holiday_from_the_csv():
    assert day_flags(date(2026, 12, 1), SCHOOL)["school_hol"]
    assert not day_flags(date(2026, 11, 20), SCHOOL)["school_hol"]
    assert day_type(date(2026, 12, 1), SCHOOL) == "weekday"  # school holidays don't change the day type


def test_johor_only_holiday():
    f = day_flags(date(2027, 1, 22), SCHOOL)  # Thaipusam: a Johor holiday, not a Singapore one
    assert f["my_ph"] and not f["sg_ph"]


def test_eve_of_a_holiday():
    assert day_flags(date(2026, 12, 24), SCHOOL)["ph_eve"]       # Thu before Christmas
    assert not day_flags(date(2026, 12, 25), SCHOOL)["ph_eve"]   # the holiday itself is not its own eve


def test_last_day_of_a_long_weekend():
    # Christmas Fri 25 Dec 2026 + weekend: the last day off is Sun 27 Dec
    assert [day_flags(date(2026, 12, d), SCHOOL)["ph_last_day"] for d in (25, 26, 27, 28)] == [False, False, True, False]
    # Deepavali Sun 8 Nov 2026, observed Mon 9 Nov: the last day off is Mon 9 Nov
    assert day_flags(date(2026, 11, 9), SCHOOL)["ph_last_day"]
    assert not day_flags(date(2026, 11, 8), SCHOOL)["ph_last_day"]
    # an ordinary Sunday is the last day of a weekend, not of a long weekend
    assert not day_flags(date(2026, 10, 18), SCHOOL)["ph_last_day"]


def test_special_days_are_kept_out_of_typical():
    assert is_special(date(2026, 12, 25), SCHOOL)       # holiday
    assert is_special(date(2026, 12, 24), SCHOOL)       # eve
    assert is_special(date(2026, 12, 27), SCHOOL)       # last day of a long weekend
    assert not is_special(date(2026, 12, 1), SCHOOL)    # school holiday only: still a normal day
    assert not is_special(date(2026, 10, 13), SCHOOL)


def test_csv_with_only_a_header(tmp_path):
    p = tmp_path / "school_holidays.csv"
    p.write_text("country,start,end,name\n", encoding="utf-8")
    assert load_school_holidays(p) == []
    p.write_text("country,start,end,name\nMY,2026-12-19,2027-01-10,test range (made up)\n", encoding="utf-8")
    assert load_school_holidays(p) == [{"country": "MY", "start": date(2026, 12, 19), "end": date(2027, 1, 10),
                                        "name": "test range (made up)"}]
    assert load_school_holidays(tmp_path / "missing.csv") == []
