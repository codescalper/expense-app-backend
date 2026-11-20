"""Transactional email.

Request handlers never talk to SMTP directly: they enqueue a ``send_email`` job in the same
database transaction (outbox pattern). That keeps responses fast and lets failed sends retry.
When an address is unknown/ineligible, the same request enqueues a ``noop_email`` job instead,
so both paths do the same database work (no timing signal for enumeration).

Payloads carry an ``expires_at``: the worker drops (and wipes) jobs that weren't delivered
before their one-time code expired, so plaintext codes don't linger in ``background_jobs``.

User-supplied names are only put into emails sent to *verified* addresses: anyone can register
someone else's address, and the name would otherwise be attacker-controlled text delivered to
the victim's inbox.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from email.message import EmailMessage as MimeMessage
from typing import Any

import aiosmtplib

from app.core.config import Settings

SEND_EMAIL_JOB = "send_email"
NOOP_EMAIL_JOB = "noop_email"  # enqueued for unknown addresses; the worker discards it


@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    subject: str
    body: str

    def to_payload(self, expires_at: datetime) -> dict[str, Any]:
        return {**asdict(self), "expires_at": expires_at.isoformat()}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> OutgoingEmail:
        return cls(
            to=str(payload["to"]), subject=str(payload["subject"]), body=str(payload["body"])
        )


def _greeting(name: str | None) -> str:
    return f"Hi {name},\n\n" if name else "Hi,\n\n"


def verification_email(to: str, code: str, minutes: int) -> OutgoingEmail:
    """Sent to a not-yet-verified address, so it never includes the registrant's name."""
    return OutgoingEmail(
        to=to,
        subject=f"{code} is your Family Budget verification code",
        body=(
            "Hi,\n\n"
            f"Your verification code is: {code}\n\n"
            f"Enter it in the Family Budget app within {minutes} minutes.\n"
            "If you didn't create an account, you can ignore this email.\n"
        ),
    )


def password_reset_email(to: str, name: str | None, code: str, minutes: int) -> OutgoingEmail:
    """``name`` must be None unless the address is verified."""
    return OutgoingEmail(
        to=to,
        subject=f"{code} is your Family Budget password reset code",
        body=(
            _greeting(name) + f"Your password reset code is: {code}\n\n"
            f"It expires in {minutes} minutes. Resetting your password signs you out "
            "of every device.\nIf you didn't ask for this, ignore this email — your "
            "password stays the same.\n"
        ),
    )


def account_exists_email(to: str, name: str) -> OutgoingEmail:
    return OutgoingEmail(
        to=to,
        subject="Someone tried to register with your email",
        body=(
            f"Hi {name},\n\n"
            "Someone tried to create a Family Budget account with this email address, "
            "but you already have one. If it was you, sign in or use 'Forgot password'.\n"
            "If it wasn't you, no action is needed.\n"
        ),
    )


async def deliver_via_smtp(message: OutgoingEmail, settings: Settings) -> None:
    mime = MimeMessage()
    mime["From"] = settings.email_from
    mime["To"] = message.to
    mime["Subject"] = message.subject
    mime.set_content(message.body)
    await aiosmtplib.send(
        mime,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username or None,
        password=settings.smtp_password.get_secret_value() if settings.smtp_password else None,
        start_tls=settings.smtp_starttls,
        use_tls=settings.smtp_use_tls,
        timeout=20,
    )
