"""Periodic UPI work: remind initiators about payments they never confirmed.

Idempotent through notification dedupe keys (``upi:<attempt id>:pending:1|2``), so several
workers or re-runs never duplicate a reminder. Nothing is ever completed, failed or cancelled
automatically — only the user can say what happened to a payment.
"""

from __future__ import annotations

from datetime import timedelta

from app.core.config import Settings
from app.core.security import utcnow
from app.db.session import Database
from app.services.upi import UpiService
from app.workers.jobs import PeriodicTask

UPI_REMINDER_INTERVAL = timedelta(minutes=5)


async def run_upi_reminders(db: Database, _settings: Settings) -> None:
    async with db.sessionmaker() as session:
        await UpiService.send_pending_reminders(session, utcnow())


PERIODIC: list[PeriodicTask] = [
    PeriodicTask(name="upi_pending_reminders", every=UPI_REMINDER_INTERVAL, run=run_upi_reminders),
]
