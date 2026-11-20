"""Infrastructure tables: audit log, rate-limit counters and the background job queue.

These replace Redis — PostgreSQL is the only stateful dependency.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Enum, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UUIDPkMixin


class AuditLog(UUIDPkMixin, CreatedAtMixin, Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_actor_created", "actor_user_id", "created_at"),
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
    )

    # No FK: audit rows must outlive the rows they describe.
    actor_user_id: Mapped[uuid.UUID | None]
    family_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[uuid.UUID | None]
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    details: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )


class RateLimitCounter(Base):
    """Fixed-window counters. Incremented atomically with INSERT ... ON CONFLICT."""

    __tablename__ = "rate_limit_counters"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(primary_key=True)
    count: Mapped[int] = mapped_column(default=0)
    expires_at: Mapped[datetime] = mapped_column(index=True)


class JobStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class BackgroundJob(UUIDPkMixin, CreatedAtMixin, Base):
    """Durable job queue. Workers claim rows with SELECT ... FOR UPDATE SKIP LOCKED."""

    __tablename__ = "background_jobs"
    __table_args__ = (
        Index(
            "ix_background_jobs_runnable",
            "run_at",
            postgresql_where=text("status = 'pending'"),
        ),
        # Lets callers enqueue idempotently (e.g. one reminder per bill per period).
        Index(
            "uq_background_jobs_dedupe_key",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL"),
        ),
    )

    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", values_callable=lambda e: [m.value for m in e]),
        default=JobStatus.PENDING,
        server_default=JobStatus.PENDING.value,
    )
    run_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    attempts: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(default=5, server_default=text("5"))
    locked_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
