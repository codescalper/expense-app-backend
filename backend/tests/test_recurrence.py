"""Pure recurrence math (app/services/recurrence.py) — no database needed."""

from __future__ import annotations

from datetime import date

import pytest

from app.models.enums import RecurrenceFrequency as F
from app.services.recurrence import Recurrence, add_months, parse_period_key, period_key


def dates(rec: Recurrence, n: int) -> list[date]:
    return [rec.nth(i) for i in range(n)]


def test_add_months_clamps_to_month_end():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)
    assert add_months(date(2026, 1, 31), 3) == date(2026, 4, 30)
    assert add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)
    assert add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)
    assert add_months(date(2026, 11, 20), 24) == date(2028, 11, 20)


def test_monthly_from_31st_never_drifts():
    rec = Recurrence(F.MONTHLY, 1, date(2026, 1, 31))
    assert dates(rec, 6) == [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
        date(2026, 5, 31),
        date(2026, 6, 30),
    ]
    assert rec.nth(12) == date(2027, 1, 31)
    assert rec.nth(25) == date(2028, 2, 29)  # leap year


def test_monthly_from_30th_and_29th_in_february():
    assert Recurrence(F.MONTHLY, 1, date(2027, 1, 30)).nth(1) == date(2027, 2, 28)
    assert Recurrence(F.MONTHLY, 1, date(2027, 1, 29)).nth(1) == date(2027, 2, 28)
    assert Recurrence(F.MONTHLY, 1, date(2028, 1, 29)).nth(1) == date(2028, 2, 29)
    assert Recurrence(F.MONTHLY, 1, date(2027, 1, 30)).nth(2) == date(2027, 3, 30)


def test_yearly_feb_29():
    rec = Recurrence(F.YEARLY, 1, date(2028, 2, 29))
    assert dates(rec, 5) == [
        date(2028, 2, 29),
        date(2029, 2, 28),
        date(2030, 2, 28),
        date(2031, 2, 28),
        date(2032, 2, 29),
    ]
    assert Recurrence(F.YEARLY, 4, date(2028, 2, 29)).nth(1) == date(2032, 2, 29)


def test_intervals():
    assert dates(Recurrence(F.DAILY, 3, date(2026, 12, 30)), 3) == [
        date(2026, 12, 30),
        date(2027, 1, 2),
        date(2027, 1, 5),
    ]
    assert dates(Recurrence(F.WEEKLY, 2, date(2026, 11, 20)), 3) == [
        date(2026, 11, 20),
        date(2026, 12, 4),
        date(2026, 12, 18),
    ]
    assert dates(Recurrence(F.MONTHLY, 3, date(2026, 11, 30)), 4) == [
        date(2026, 11, 30),
        date(2027, 2, 28),
        date(2027, 5, 30),
        date(2027, 8, 30),
    ]
    assert dates(Recurrence(F.YEARLY, 2, date(2026, 6, 1)), 3) == [
        date(2026, 6, 1),
        date(2028, 6, 1),
        date(2030, 6, 1),
    ]


def test_end_date_is_inclusive_and_bounds_queries():
    rec = Recurrence(F.MONTHLY, 1, date(2026, 1, 15), end_date=date(2026, 4, 15))
    assert list(rec.iter_from(date(2000, 1, 1))) == [
        date(2026, 1, 15),
        date(2026, 2, 15),
        date(2026, 3, 15),
        date(2026, 4, 15),
    ]
    assert rec.next_after(date(2026, 4, 15)) is None
    assert rec.first_on_or_after(date(2026, 4, 15)) == date(2026, 4, 15)
    assert rec.candidate_after(date(2026, 4, 15)) == date(2026, 5, 15)
    one_shot = Recurrence(F.DAILY, 1, date(2026, 5, 1), end_date=date(2026, 5, 1))
    assert list(one_shot.iter_from(date(2026, 1, 1))) == [date(2026, 5, 1)]


def test_next_due_and_first_on_or_after():
    rec = Recurrence(F.MONTHLY, 1, date(2026, 1, 31))
    assert rec.first_on_or_after(date(2026, 2, 1)) == date(2026, 2, 28)
    assert rec.first_on_or_after(date(2026, 2, 28)) == date(2026, 2, 28)
    assert rec.next_after(date(2026, 2, 28)) == date(2026, 3, 31)
    assert rec.next_after(date(2025, 6, 1)) == date(2026, 1, 31)  # before the start
    assert rec.next_after(date(2026, 1, 30)) == date(2026, 1, 31)
    weekly = Recurrence(F.WEEKLY, 1, date(2026, 11, 2))  # a Monday
    assert weekly.next_after(date(2026, 11, 20)) == date(2026, 11, 23)
    assert weekly.is_occurrence(date(2026, 11, 16)) and not weekly.is_occurrence(date(2026, 11, 17))


def test_between_and_fast_skip_matches_brute_force():
    cases = [
        Recurrence(F.DAILY, 1, date(2016, 1, 1)),
        Recurrence(F.DAILY, 7, date(2016, 1, 3)),
        Recurrence(F.WEEKLY, 3, date(2016, 2, 29)),
        Recurrence(F.MONTHLY, 1, date(2016, 1, 31)),
        Recurrence(F.MONTHLY, 5, date(2016, 3, 31)),
        Recurrence(F.YEARLY, 1, date(2016, 2, 29)),
    ]
    probes = [date(2016, 1, 1), date(2019, 3, 1), date(2024, 2, 29), date(2026, 11, 20)]
    for rec in cases:
        everything: list[date] = []
        while not everything or everything[-1] <= date(2030, 1, 1):
            everything.append(rec.nth(len(everything)))
        for probe in probes:
            expected = next(d for d in everything if d >= probe)
            assert rec.first_on_or_after(probe) == expected, (rec, probe)
        window = rec.between(date(2026, 1, 1), date(2026, 12, 31))
        assert window == [d for d in everything if date(2026, 1, 1) <= d <= date(2026, 12, 31)]
    assert Recurrence(F.DAILY, 1, date(2026, 1, 1)).between(
        date(2026, 1, 1), date(2026, 12, 31), limit=5
    ) == [date(2026, 1, d) for d in range(1, 6)]
    assert (
        Recurrence(F.DAILY, 1, date(2026, 1, 1)).between(date(2026, 2, 1), date(2026, 1, 1)) == []
    )


def test_period_keys_are_unique_iso_dates():
    for rec in (
        Recurrence(F.DAILY, 1, date(2026, 1, 1)),
        Recurrence(F.WEEKLY, 2, date(2026, 1, 1)),
        Recurrence(F.MONTHLY, 1, date(2026, 1, 31)),
        Recurrence(F.YEARLY, 1, date(2024, 2, 29)),
    ):
        keys = [period_key(rec.nth(i)) for i in range(300)]
        assert len(set(keys)) == len(keys)
        assert keys == sorted(keys)  # ISO dates sort chronologically
    assert period_key(date(2026, 11, 20)) == "2026-11-20"
    assert parse_period_key("2026-11-20") == date(2026, 11, 20)


def test_invalid_rules_rejected():
    with pytest.raises(ValueError):
        Recurrence(F.DAILY, 0, date(2026, 1, 1))
    with pytest.raises(ValueError):
        Recurrence(F.DAILY, 1, date(2026, 1, 2), end_date=date(2026, 1, 1))
    with pytest.raises(ValueError):
        Recurrence(F.DAILY, 1, date(2026, 1, 1)).nth(-1)
