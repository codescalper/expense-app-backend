"""Transactional email delivery (outbox → SMTP)."""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.services.email import NOOP_EMAIL_JOB, SEND_EMAIL_JOB, OutgoingEmail, deliver_via_smtp
from app.workers.jobs import JobHandler


async def handle_send_email(payload: dict[str, Any], settings: Settings) -> None:
    await deliver_via_smtp(OutgoingEmail.from_payload(payload), settings)


async def handle_noop_email(_payload: dict[str, Any], _settings: Settings) -> None:
    """Placeholder job enqueued for unknown/ineligible addresses so that forgot-password and
    resend-verification do identical DB work either way. Nothing is sent."""


HANDLERS: dict[str, JobHandler] = {
    SEND_EMAIL_JOB: handle_send_email,
    NOOP_EMAIL_JOB: handle_noop_email,
}
