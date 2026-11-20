"""Expo push sender: delivers pending notifications every few seconds (see services/push.py)."""

from __future__ import annotations

from datetime import timedelta

from app.core.config import Settings
from app.db.session import Database
from app.services.push import send_due_pushes
from app.workers.jobs import PeriodicTask


async def run_push_sender(db: Database, settings: Settings) -> None:
    await send_due_pushes(db, settings)


PERIODIC: list[PeriodicTask] = [
    PeriodicTask(name="push_sender", every=timedelta(seconds=10), run=run_push_sender),
]
