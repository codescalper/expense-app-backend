"""Background worker backed by PostgreSQL (no Redis).

Run with ``python -m app.workers.worker``. Several workers can run at once: jobs are claimed
with ``SELECT ... FOR UPDATE SKIP LOCKED`` so each job is processed by one worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import signal
import uuid
from collections.abc import Collection, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.security import utcnow
from app.db.session import Database
from app.models.system import BackgroundJob, JobStatus
from app.repositories.system import JobRepository, purge_expired_auth_data
from app.workers.jobs import JobHandler, PeriodicTask, discover

log = get_logger("app.worker")

_EMAIL_RE = re.compile(r"[^\s<>@,;:\"']+@[^\s<>@,;:\"']+")
HOUSEKEEPING_INTERVAL = timedelta(minutes=30)
STALE_JOB_AFTER = timedelta(minutes=10)


HANDLERS, PERIODIC = discover()


def _is_expired(payload: dict[str, Any], at: datetime | None = None) -> bool:
    """Jobs may carry an ISO-8601 ``expires_at``; past it they are dropped, not run."""
    raw = payload.get("expires_at")
    if not isinstance(raw, str):
        return False
    try:
        expires_at = datetime.fromisoformat(raw)
    except ValueError:
        return True  # malformed: fail closed (the payload is wiped)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= (at or utcnow())


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(15 * 2 ** (attempts - 1), 3600))


async def process_due_jobs(
    db: Database,
    settings: Settings,
    handlers: dict[str, JobHandler] | None = None,
    *,
    batch_size: int | None = None,
    job_ids: Sequence[uuid.UUID] | None = None,
    kinds: Collection[str] | None = None,
) -> int:
    """Claim and run one batch of due jobs. Returns how many jobs were attempted.

    ``job_ids``/``kinds`` restrict the claim (inline email delivery runs exactly the jobs its
    request enqueued); anything not claimed here stays queued for the worker / cron tick."""
    handlers = handlers if handlers is not None else HANDLERS
    limit = batch_size or settings.worker_batch_size
    if job_ids is not None:
        if not job_ids:
            return 0
        limit = len(job_ids)
    async with db.sessionmaker() as session:
        jobs = await JobRepository(session).claim_batch(utcnow(), limit, ids=job_ids, kinds=kinds)
        claimed = [(j.id, j.kind, dict(j.payload), j.attempts, j.max_attempts) for j in jobs]
        await session.commit()

    for job_id, kind, payload, attempts, max_attempts in claimed:
        handler = handlers.get(kind)
        error: str | None = None
        expired = _is_expired(payload)
        if expired:
            # e.g. an email whose one-time code has already expired: never send it late.
            error = "expired before it could run"
        elif handler is None:
            error = f"no handler for job kind {kind!r}"
        else:
            try:
                await handler(payload, settings)
            except Exception as exc:  # noqa: BLE001 - any failure is retried / recorded
                # SMTP errors quote the recipient: keep addresses out of logs and last_error.
                error = f"{type(exc).__name__}: {_EMAIL_RE.sub('<email>', str(exc))}"[:2000]
        async with db.sessionmaker() as session:
            job = await session.get(BackgroundJob, job_id)
            if job is None:
                continue
            now = utcnow()
            if error is None:
                job.status = JobStatus.DONE
                job.finished_at = now
                job.payload = {}  # wipe payloads (email bodies contain one-time codes)
                log.info("job_done", job_id=str(job_id), kind=kind)
            elif (
                expired
                or attempts >= max_attempts
                or handler is None
                # the retry would only run after expiry: give up now and wipe the payload
                or _is_expired(payload, at=now + _backoff(attempts))
            ):
                job.status = JobStatus.FAILED
                job.finished_at = now
                job.last_error = error
                job.payload = {}  # failed jobs never keep one-time codes around either
                log.error("job_failed", job_id=str(job_id), kind=kind, error=error)
            else:
                job.status = JobStatus.PENDING
                job.run_at = now + _backoff(attempts)
                job.locked_at = None
                job.last_error = error
                log.warning("job_retry", job_id=str(job_id), kind=kind, attempts=attempts)
            await session.commit()
    return len(claimed)


async def run_housekeeping(db: Database) -> dict[str, int]:
    async with db.sessionmaker() as session:
        now = utcnow()
        counts = await purge_expired_auth_data(session, now)
        counts["requeued_stale_jobs"] = await JobRepository(session).requeue_stale(
            now, STALE_JOB_AFTER
        )
        await session.commit()
    return counts


async def run_due_periodic(
    db: Database, settings: Settings, tasks: list[PeriodicTask], next_run: dict[str, datetime]
) -> None:
    """Run each periodic task whose interval has elapsed. One task failing never stops others."""
    for task in tasks:
        if utcnow() < next_run.get(task.name, utcnow()):
            continue
        try:
            await task.run(db, settings)
            log.info("periodic_done", task=task.name)
        except Exception:
            log.exception("periodic_failed", task=task.name)
        next_run[task.name] = utcnow() + task.every


async def run_forever() -> None:
    settings = get_settings()
    configure_logging(settings.environment)
    db = Database(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # not available on Windows
            loop.add_signal_handler(sig, stop.set)

    log.info(
        "worker_started",
        poll_seconds=settings.worker_poll_interval_seconds,
        job_kinds=sorted(HANDLERS),
        periodic=[t.name for t in PERIODIC],
    )
    next_housekeeping = utcnow()
    next_run: dict[str, datetime] = {task.name: utcnow() for task in PERIODIC}
    try:
        while not stop.is_set():
            try:
                if utcnow() >= next_housekeeping:
                    log.info("housekeeping", **await run_housekeeping(db))
                    next_housekeeping = utcnow() + HOUSEKEEPING_INTERVAL
                await run_due_periodic(db, settings, PERIODIC, next_run)
                processed = await process_due_jobs(db, settings)
            except Exception:
                log.exception("worker_loop_error")
                processed = 0
            if processed == 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), settings.worker_poll_interval_seconds)
    finally:
        await db.dispose()
        log.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(run_forever())
