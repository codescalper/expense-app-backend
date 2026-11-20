"""Recurrence date math for recurring rules (spec §9). Pure functions — no I/O, no clock.

A schedule is ``frequency`` (daily / weekly / monthly / yearly) times ``interval`` (≥ 1) anchored at
``start_date`` and optionally bounded by ``end_date`` (inclusive).

Occurrence *n* (``n = 0, 1, 2 …``) is always computed **from the anchor**, never from the
previous occurrence, so month-end clamping never drifts:

* monthly from Jan 31 → Jan 31, Feb 28 (29 in leap years), Mar 31, Apr 30, May 31 …
* yearly from Feb 29 → Feb 28 in non-leap years, Feb 29 again in leap years.

Each occurrence is identified by its ``period_key`` — the scheduled date in ISO format
(``"2026-11-20"``). Distinct occurrences of one schedule always have distinct dates, so the key is
unique per occurrence for any frequency/interval; ISO dates also sort lexicographically.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta

from app.models.enums import RecurrenceFrequency

MAX_INTERVAL = 365


def add_months(anchor: date, months: int) -> date:
    """``anchor`` shifted by ``months`` calendar months, clamping the day to the month's end."""
    index = anchor.year * 12 + (anchor.month - 1) + months
    year, month0 = divmod(index, 12)
    month = month0 + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def period_key(occurrence: date) -> str:
    """Idempotency key of the occurrence scheduled on ``occurrence`` (its ISO date)."""
    return occurrence.isoformat()


def parse_period_key(key: str) -> date:
    """Inverse of :func:`period_key`."""
    return date.fromisoformat(key)


@dataclass(frozen=True)
class Recurrence:
    frequency: RecurrenceFrequency
    interval: int
    start_date: date
    end_date: date | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.interval <= MAX_INTERVAL:
            raise ValueError(f"interval must be between 1 and {MAX_INTERVAL}")
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")

    # -- core ------------------------------------------------------------------------------

    def nth(self, n: int) -> date:
        """The *n*-th scheduled date (``n = 0`` is ``start_date``), ignoring ``end_date``."""
        if n < 0:
            raise ValueError("n must be >= 0")
        step = n * self.interval
        match self.frequency:
            case RecurrenceFrequency.DAILY:
                return self.start_date + timedelta(days=step)
            case RecurrenceFrequency.WEEKLY:
                return self.start_date + timedelta(weeks=step)
            case RecurrenceFrequency.MONTHLY:
                return add_months(self.start_date, step)
            case RecurrenceFrequency.YEARLY:
                return add_months(self.start_date, 12 * step)
        raise ValueError(f"unknown frequency {self.frequency!r}")  # pragma: no cover

    def within_end(self, day: date) -> bool:
        return self.end_date is None or day <= self.end_date

    def _index_lower_bound(self, day: date) -> int:
        """An index ``n`` with ``nth(n) <= day`` or ``n == 0`` — never past the first occurrence
        on/after ``day``, so callers can start scanning there instead of at 0."""
        if day <= self.start_date:
            return 0
        match self.frequency:
            case RecurrenceFrequency.DAILY:
                return (day - self.start_date).days // self.interval
            case RecurrenceFrequency.WEEKLY:
                return (day - self.start_date).days // (7 * self.interval)
            case RecurrenceFrequency.MONTHLY | RecurrenceFrequency.YEARLY:
                months = (day.year - self.start_date.year) * 12 + day.month - self.start_date.month
                per = self.interval * (12 if self.frequency is RecurrenceFrequency.YEARLY else 1)
                # Clamping can make nth(k) fall a few days early but never into an earlier
                # month, so (months // per) - 1 is always safely at or before ``day``.
                return max(0, months // per - 1)
        raise ValueError(f"unknown frequency {self.frequency!r}")  # pragma: no cover

    # -- queries ---------------------------------------------------------------------------

    def iter_from(self, day: date) -> Iterator[date]:
        """Occurrences on or after ``day`` (respecting ``end_date``), in order."""
        n = self._index_lower_bound(day)
        while True:
            occurrence = self.nth(n)
            if not self.within_end(occurrence):
                return
            if occurrence >= day:
                yield occurrence
            n += 1

    def first_on_or_after(self, day: date) -> date | None:
        """The first occurrence ``>= day``; ``None`` if the schedule has ended by then."""
        return next(self.iter_from(day), None)

    def next_after(self, day: date) -> date | None:
        """The first occurrence strictly after ``day`` (the next due date after ``day``)."""
        return self.first_on_or_after(day + timedelta(days=1))

    def between(self, first: date, last: date, *, limit: int | None = None) -> list[date]:
        """Occurrences with ``first <= date <= last`` (at most ``limit`` of them)."""
        out: list[date] = []
        if last < first:
            return out
        for occurrence in self.iter_from(first):
            if occurrence > last or (limit is not None and len(out) >= limit):
                break
            out.append(occurrence)
        return out

    def is_occurrence(self, day: date) -> bool:
        return self.first_on_or_after(day) == day

    def candidate_after(self, day: date) -> date:
        """The scheduled date strictly after ``day`` **ignoring** ``end_date`` — used as the
        cursor value once a schedule is exhausted (always ``> end_date``)."""
        n = self._index_lower_bound(day + timedelta(days=1))
        while (occurrence := self.nth(n)) <= day:
            n += 1
        return occurrence
