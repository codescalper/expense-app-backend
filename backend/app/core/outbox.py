"""Request-scoped record of jobs enqueued during the current request.

Used by ``EMAIL_DELIVERY=inline``: ``app.api.deps.inline_job_delivery`` starts a collection at
the beginning of each API request, ``JobRepository.enqueue`` adds every new job id to it, and a
background task delivers the collected email jobs right after the response is sent. Outside a
collecting request (worker, cron tick, tests calling services directly) nothing is recorded.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

_enqueued: ContextVar[list[uuid.UUID] | None] = ContextVar("enqueued_job_ids", default=None)


def start_collecting() -> list[uuid.UUID]:
    """Begin recording job ids for this request; returns the (live) list they go into."""
    ids: list[uuid.UUID] = []
    _enqueued.set(ids)
    return ids


def stop_collecting() -> None:
    _enqueued.set(None)


def record_enqueued(job_id: uuid.UUID) -> None:
    ids = _enqueued.get()
    if ids is not None:
        ids.append(job_id)
