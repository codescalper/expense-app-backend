"""Spending insights. Hourly: the "unusual spending" rule (services/notifications.py).

Idempotent per user and local day through the notification dedupe key, so running it every
hour (or on several workers) never produces more than one alert a day.
"""

from __future__ import annotations

from datetime import timedelta

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.session import Database
from app.services.notifications import run_unusual_spending_check
from app.workers.jobs import PeriodicTask

log = get_logger("app.insights")


async def run_insights(db: Database, _settings: Settings) -> None:
    async with db.sessionmaker() as session:
        created = await run_unusual_spending_check(session)
        await session.commit()
    if created:
        log.info("unusual_spending_alerts", created=created)


PERIODIC: list[PeriodicTask] = [
    PeriodicTask(name="unusual_spending", every=timedelta(hours=1), run=run_insights),
]
