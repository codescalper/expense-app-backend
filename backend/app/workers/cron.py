"""One bounded pass of the background work, for platforms without a long-running worker.

``POST|GET /internal/cron/tick`` (``app/api/internal.py``) calls :func:`run_tick`, which does
what ``python -m app.workers.worker`` does continuously, once:

1. drain due ``background_jobs`` (emails) in small batches;
2. run every periodic task (``workers/jobs/*.PERIODIC`` plus housekeeping) whose interval has
   elapsed since its last run, as recorded in ``periodic_task_runs``;
3. drain jobs again (periodic tasks can enqueue work).

Overlapping ticks (a slow tick, a duplicate cron delivery, an external pinger) are serialised
by a transaction-scoped PostgreSQL advisory lock, held on a dedicated connection for the whole
pass: a tick that can't take it returns immediately with ``status="locked"`` and runs nothing.
``pg_try_advisory_xact_lock`` works through a transaction-mode pooler (Neon ``-pooler``),
and if the process dies the connection closes and PostgreSQL releases the lock.

Every task is idempotent (unique keys / notification dedupe keys) and jobs are claimed with
``FOR UPDATE SKIP LOCKED``, so even running this alongside a classic worker is safe.
The pass respects a time budget (``CRON_TIME_BUDGET_SECONDS``): it stops starting new work
once the budget is nearly spent; whatever is left is picked up by the next tick.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import utcnow
from app.db.session import Database
from app.models.system import PeriodicTaskRun
from app.workers.jobs import JobHandler, PeriodicTask
from app.workers.worker import (
    HANDLERS,
    HOUSEKEEPING_INTERVAL,
    PERIODIC,
    process_due_jobs,
    run_housekeeping,
)

log = get_logger("app.cron")

CRON_LOCK_KEY = 0x46_42_43_52_4F_4E  # "FBCRON"
# A job batch can include an SMTP send (20 s timeout each); don't start one this close to the
# end of the budget. Claimed-but-unfinished jobs would otherwise wait for the stale requeue.
JOB_BATCH_RESERVE_SECONDS = 25.0
JOB_BATCH_SIZE = 5
# Don't start a periodic task with less than this much budget left.
MIN_TASK_SECONDS = 3.0
# Cron deliveries jitter (Vercel: anywhere within the scheduled minute, Hobby: the hour), so a
# task is "due" slightly early rather than skipping a whole tick: half its interval, max 60 s.
MAX_SCHEDULE_SLACK = timedelta(seconds=60)


async def _housekeeping(db: Database, _settings: Settings) -> None:
    log.info("housekeeping", **await run_housekeeping(db))


HOUSEKEEPING_TASK = PeriodicTask(
    name="housekeeping", every=HOUSEKEEPING_INTERVAL, run=_housekeeping
)


@dataclass
class TickResult:
    status: Literal["ok", "locked"] = "ok"
    jobs_processed: int = 0
    ran: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)  # due, but the time budget ran out
    duration_ms: int = 0


def schedule_slack(every: timedelta) -> timedelta:
    return min(every / 2, MAX_SCHEDULE_SLACK)


def is_due(last_run_at: datetime | None, every: timedelta, now: datetime) -> bool:
    return last_run_at is None or last_run_at + every - schedule_slack(every) <= now


async def _last_runs(db: Database) -> dict[str, datetime]:
    async with db.sessionmaker() as session:
        rows = await session.execute(select(PeriodicTaskRun.name, PeriodicTaskRun.last_run_at))
        return {name: at for name, at in rows.all()}


async def _record_run(db: Database, name: str, started_at: datetime, status: str) -> None:
    stmt = pg_insert(PeriodicTaskRun).values(name=name, last_run_at=started_at, last_status=status)
    stmt = stmt.on_conflict_do_update(
        index_elements=[PeriodicTaskRun.name],
        set_={
            "last_run_at": stmt.excluded.last_run_at,
            "last_status": stmt.excluded.last_status,
            "updated_at": func.now(),
        },
    )
    async with db.sessionmaker() as session:
        await session.execute(stmt)
        await session.commit()


async def _drain_jobs(
    db: Database,
    settings: Settings,
    handlers: dict[str, JobHandler],
    deadline: float,
    clock: Callable[[], float],
) -> int:
    total = 0
    batch = max(1, min(settings.worker_batch_size, JOB_BATCH_SIZE))
    while deadline - clock() > JOB_BATCH_RESERVE_SECONDS:
        processed = await process_due_jobs(db, settings, handlers, batch_size=batch)
        total += processed
        if processed < batch:
            break
    return total


async def run_tick(
    db: Database,
    settings: Settings,
    *,
    tasks: Sequence[PeriodicTask] | None = None,
    handlers: dict[str, JobHandler] | None = None,
    budget_seconds: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> TickResult:
    """Run one bounded pass. Never raises for a failing task (it is logged and recorded)."""
    started = clock()
    budget = settings.cron_time_budget_seconds if budget_seconds is None else budget_seconds
    deadline = started + budget
    tasks = list(tasks) if tasks is not None else [HOUSEKEEPING_TASK, *PERIODIC]
    handlers = handlers if handlers is not None else HANDLERS
    result = TickResult()

    async with db.engine.connect() as lock_conn, lock_conn.begin():
        locked = await lock_conn.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": CRON_LOCK_KEY}
        )
        if not locked:
            result.status = "locked"
            log.info("cron_tick_skipped_locked")
            return result

        result.jobs_processed += await _drain_jobs(db, settings, handlers, deadline, clock)

        last_runs = await _last_runs(db)
        for task in tasks:
            now = utcnow()
            if not is_due(last_runs.get(task.name), task.every, now):
                continue
            remaining = deadline - clock()
            if remaining < MIN_TASK_SECONDS:
                result.deferred.append(task.name)
                continue
            status = "ok"
            try:
                await asyncio.wait_for(task.run(db, settings), timeout=remaining)
                result.ran.append(task.name)
            except Exception:  # one failing task never stops the others
                log.exception("periodic_failed", task=task.name)
                result.failed.append(task.name)
                status = "failed"
            # Failed tasks also wait a full interval (like the worker): they are retried later.
            await _record_run(db, task.name, now, status)

        result.jobs_processed += await _drain_jobs(db, settings, handlers, deadline, clock)
        # Leaving the block commits, which releases the advisory lock.

    result.duration_ms = int((clock() - started) * 1000)
    log.info(
        "cron_tick_done",
        jobs=result.jobs_processed,
        ran=result.ran,
        failed=result.failed,
        deferred=result.deferred,
        duration_ms=result.duration_ms,
    )
    return result
