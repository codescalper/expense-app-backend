"""``EMAIL_DELIVERY=inline``: deliver a request's email jobs right after its response.

The request still writes its ``send_email`` / ``noop_email`` job in the same transaction as
the state change (outbox), exactly as in queue mode, so:

* commit means the job exists and rollback means it doesn't (nothing is sent for a failed
  request);
* known and unknown addresses still do identical database work in the request, and the SMTP
  conversation happens only after the response has been sent, so response timing doesn't
  reveal whether an address is registered;
* the job runs through the normal worker code path: expired jobs are dropped, payloads (which
  hold one-time codes) are wiped once done or failed, and a failed send stays queued with
  backoff for the cron tick / worker to retry.

Nothing here logs addresses, subjects or codes.
"""

from __future__ import annotations

import uuid

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import get_database
from app.services.email import NOOP_EMAIL_JOB, SEND_EMAIL_JOB

log = get_logger("app.inline_jobs")

INLINE_JOB_KINDS = frozenset({SEND_EMAIL_JOB, NOOP_EMAIL_JOB})


async def deliver_enqueued_jobs(job_ids: list[uuid.UUID]) -> None:
    """Background task: run the email jobs this request enqueued (if it committed them)."""
    if not job_ids:
        return
    # Imported lazily: the worker module discovers (imports) every job module.
    from app.workers.worker import process_due_jobs

    try:
        await process_due_jobs(
            get_database(), get_settings(), job_ids=list(job_ids), kinds=INLINE_JOB_KINDS
        )
    except Exception:  # the jobs stay queued; the cron tick retries them
        log.exception("inline_delivery_failed", jobs=len(job_ids))
