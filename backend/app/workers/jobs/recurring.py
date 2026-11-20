"""Periodic recurring-rule work: generate due pending transactions, then bill reminders.

Both steps are idempotent (unique (recurring_id, period_key) and notification dedupe keys), so
running several workers or re-running after a crash never duplicates anything.
"""

from __future__ import annotations

from datetime import timedelta

from app.core.config import Settings
from app.core.security import utcnow
from app.db.session import Database
from app.services.recurring import RecurringService
from app.workers.jobs import PeriodicTask

RECURRING_INTERVAL = timedelta(minutes=15)


async def run_recurring(db: Database, _settings: Settings) -> None:
    now = utcnow()
    async with db.sessionmaker() as session:
        await RecurringService.generate_due(session, now)
    async with db.sessionmaker() as session:
        await RecurringService.send_reminders(session, now)


PERIODIC: list[PeriodicTask] = [
    PeriodicTask(name="recurring", every=RECURRING_INTERVAL, run=run_recurring),
]
