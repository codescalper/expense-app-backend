from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import redact
from app.models.system import AuditLog, BackgroundJob, JobStatus, RateLimitCounter
from app.models.user import RefreshToken, UserSession, UserToken


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def record(
        self,
        action: str,
        *,
        actor_user_id: uuid.UUID | None,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
        family_id: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            AuditLog(
                action=action,
                actor_user_id=actor_user_id,
                entity_type=entity_type,
                entity_id=entity_id,
                family_id=family_id,
                ip_address=ip_address,
                user_agent=(user_agent or "")[:400] or None,
                details=redact(details or {}),
            )
        )


class JobRepository:
    """PostgreSQL job queue (replaces Redis/arq)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        run_at: datetime | None = None,
        max_attempts: int | None = None,
        dedupe_key: str | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "kind": kind,
            "payload": payload,
            "max_attempts": max_attempts or get_settings().job_max_attempts,
            "dedupe_key": dedupe_key,
        }
        if run_at is not None:
            values["run_at"] = run_at
        stmt = pg_insert(BackgroundJob).values(**values)
        if dedupe_key is not None:
            stmt = stmt.on_conflict_do_nothing(
                index_elements=[BackgroundJob.dedupe_key],
                index_where=BackgroundJob.dedupe_key.is_not(None),
            )
        await self.session.execute(stmt)

    async def claim_batch(self, now: datetime, limit: int) -> list[BackgroundJob]:
        """Lock runnable jobs; concurrent workers skip rows another worker holds."""
        jobs = list(
            await self.session.scalars(
                select(BackgroundJob)
                .where(BackgroundJob.status == JobStatus.PENDING, BackgroundJob.run_at <= now)
                .order_by(BackgroundJob.run_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        for job in jobs:
            job.status = JobStatus.RUNNING
            job.locked_at = now
            job.attempts += 1
        return jobs

    async def requeue_stale(self, now: datetime, stale_after: timedelta) -> int:
        """Jobs left RUNNING by a crashed worker go back to PENDING."""
        result = await self.session.execute(
            update(BackgroundJob)
            .where(
                BackgroundJob.status == JobStatus.RUNNING,
                BackgroundJob.locked_at < now - stale_after,
            )
            .values(status=JobStatus.PENDING, locked_at=None)
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)


async def purge_expired_auth_data(session: AsyncSession, now: datetime) -> dict[str, int]:
    """Housekeeping: drop dead sessions/tokens, stale counters and old finished jobs."""
    counts: dict[str, int] = {}
    grace = now - timedelta(days=30)
    statements = {
        "sessions": delete(UserSession).where(
            or_(UserSession.expires_at < grace, UserSession.revoked_at < grace)
        ),
        "refresh_rows": delete(RefreshToken).where(RefreshToken.expires_at < now),
        "email_code_rows": delete(UserToken).where(
            or_(UserToken.expires_at < now, UserToken.consumed_at.is_not(None))
        ),
        "rate_limit_counters": delete(RateLimitCounter).where(RateLimitCounter.expires_at < now),
        "jobs": delete(BackgroundJob).where(
            BackgroundJob.status.in_([JobStatus.DONE, JobStatus.FAILED]),
            BackgroundJob.finished_at < now - timedelta(days=7),
        ),
    }
    for name, stmt in statements.items():
        result = await session.execute(stmt.execution_options(synchronize_session=False))
        counts[name] = int(getattr(result, "rowcount", 0) or 0)
    return counts
