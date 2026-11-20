"""Periodic budget-alert evaluation (every 10 minutes).

The real work is :meth:`app.services.budgets.BudgetService.evaluate_alerts`, which is
idempotent (notification dedupe keys), so overlapping or repeated runs never duplicate alerts.
"""

from __future__ import annotations

from datetime import timedelta

from app.core.config import Settings
from app.core.security import utcnow
from app.db.session import Database
from app.services.budgets import BudgetService
from app.workers.jobs import PeriodicTask


async def evaluate_budget_alerts(db: Database, settings: Settings) -> None:
    async with db.sessionmaker() as session:
        await BudgetService.evaluate_alerts(session, utcnow())


PERIODIC = [
    PeriodicTask(name="budget_alerts", every=timedelta(minutes=10), run=evaluate_budget_alerts)
]
