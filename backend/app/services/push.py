"""Expo push delivery for stored notifications (run by the worker, see workers/jobs/push.py).

Flow, per batch of at most ``PUSH_BATCH_SIZE`` notifications:

1. Claim notifications with ``push_status = pending`` and ``scheduled_for <= now``
   (``FOR UPDATE SKIP LOCKED``: several workers never send the same row). The rows stay locked
   until the batch is committed, after the HTTP calls (bounded by ``EXPO_PUSH_TIMEOUT_SECONDS``).
2. ``PUSH_ENABLED=false`` → every claimed row becomes ``skipped``.
3. Load the users' enabled device tokens. A token whose bound session is revoked, expired, past
   its absolute lifetime or gone is disabled and not used. Inactive/deleted users get nothing.
   No usable token → ``skipped``.
4. Send one message per (notification, token), at most ``EXPO_MAX_MESSAGES`` per request.
5. Tickets: any ``ok`` → ``sent`` (+ ``sent_at``). ``DeviceNotRegistered`` disables that token.
   Other ticket errors (or a 4xx request rejection) → ``failed``. A notification whose only
   problem was unregistered devices → ``skipped`` (nobody left to deliver to).
6. Network errors, timeouts, HTTP 429/5xx are *transient*: the row stays ``pending`` and is
   retried on the next run. The attempt count lives in ``data._push_attempts``; after
   ``MAX_TRANSIENT_ATTEMPTS`` consecutive failures for a notification that has been due for
   longer than ``GIVE_UP_AFTER`` it becomes ``failed`` (so a dead Expo endpoint never loops
   forever).

Logs never contain message bodies or whole tokens (see :func:`mask_token`).
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import select

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import utcnow
from app.db.session import Database
from app.models.enums import NotificationCategory, PushStatus
from app.models.notification import DeviceToken, Notification
from app.models.user import User, UserSession
from app.repositories.notifications import (
    PUSH_ATTEMPTS_KEY,
    DeviceTokenRepository,
    NotificationRepository,
)

log = get_logger("app.push")

PUSH_BATCH_SIZE = 100  # notifications claimed per transaction
EXPO_MAX_MESSAGES = 100  # Expo's limit per push/send request
MAX_BATCHES_PER_RUN = 10
MAX_TRANSIENT_ATTEMPTS = 3
GIVE_UP_AFTER = timedelta(hours=1)
DEVICE_NOT_REGISTERED = "DeviceNotRegistered"

# --- deep links ----------------------------------------------------------------------------

# The mobile app's URL scheme (mobile/app.json "scheme"); see also services/family_emails.py.
APP_SCHEME = "familybudget://"
_SAFE_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def _id(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str) and _SAFE_ID.match(value):
        return value
    return None


def deep_link(
    category: NotificationCategory,
    data: dict[str, Any],
    *,
    family_id: uuid.UUID | None = None,
) -> str:
    """App route for a notification, put in the push payload's ``data.url`` and in the API.

    A producer may set ``data["url"]`` itself (must start with ``familybudget://``); otherwise
    the link is derived from the category and the ids in ``data``. Unknown/malformed ids fall
    back to the section's list screen.
    """
    explicit = data.get("url")
    if isinstance(explicit, str) and explicit.startswith(APP_SCHEME) and len(explicit) <= 500:
        return explicit
    fam = _id(data, "family_id") or (str(family_id) if family_id else None)

    def link(path: str) -> str:
        return APP_SCHEME + path

    match category:
        case NotificationCategory.BUDGET_ALERT:
            budget = _id(data, "budget_id")
            return link(f"budget/{budget}" if budget else "budgets")
        case NotificationCategory.BILL_UPCOMING | NotificationCategory.RECURRING_REMINDER:
            rec = _id(data, "recurring_id")
            return link(f"recurring/{rec}" if rec else "recurring")
        case NotificationCategory.PAYMENT_CONFIRMATION | NotificationCategory.UPI_PENDING:
            if attempt := _id(data, "upi_attempt_id"):
                return link(f"upi/{attempt}")
            tx = _id(data, "transaction_id")
            return link(f"transaction/{tx}" if tx else "transactions")
        case NotificationCategory.FAMILY_INVITATION:
            inv = _id(data, "invitation_id")
            return link(f"family/invitations/{inv}" if inv else "family/invitations")
        case NotificationCategory.FAMILY_MEMBER_JOINED:
            return link(f"family/{fam}" if fam else "family")
        case NotificationCategory.FAMILY_SHARED_EXPENSE:
            if tx := _id(data, "transaction_id"):
                return link(f"transaction/{tx}")
            return link(f"family/{fam}" if fam else "transactions")
        case NotificationCategory.SAVINGS_MILESTONE:
            goal = _id(data, "goal_id")
            return link(f"savings/{goal}" if goal else "savings")
        case NotificationCategory.UNUSUAL_SPENDING:
            day = data.get("date")
            if isinstance(day, str):
                try:
                    return link("transactions?date=" + quote(date.fromisoformat(day).isoformat()))
                except ValueError:
                    pass
            return link("transactions")
        case _:
            return link("notifications")


def public_data(data: dict[str, Any]) -> dict[str, Any]:
    """``data`` without the internal ``_``-prefixed bookkeeping keys."""
    return {k: v for k, v in data.items() if not k.startswith("_")}


def mask_token(token: str) -> str:
    """``ExponentPushToken[…abcd]`` — enough to tell devices apart, useless to an attacker."""
    prefix, _, rest = token.partition("[")
    inner = rest.rstrip("]")
    return f"{prefix}[…{inner[-4:]}]" if inner else "…"


# --- sending -------------------------------------------------------------------------------


@dataclass
class PushRunResult:
    claimed: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    retrying: int = 0
    devices_disabled: int = 0
    requests: int = 0

    def add(self, other: PushRunResult) -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))


@dataclass
class _Outcome:
    ok: int = 0
    errors: int = 0
    unregistered: int = 0
    transient: bool = False


@dataclass
class _Message:
    notification: Notification
    token: DeviceToken
    payload: dict[str, Any] = field(repr=False)


class _TransientError(Exception):
    pass


class _RejectedError(Exception):
    pass


def _session_live(user_session: UserSession | None, now: datetime, settings: Settings) -> bool:
    if user_session is None:
        return False
    oldest_allowed = now - timedelta(days=settings.session_absolute_lifetime_days)
    return (
        user_session.revoked_at is None
        and user_session.expires_at > now
        and user_session.created_at > oldest_allowed
    )


def _payload(notification: Notification, token: str) -> dict[str, Any]:
    data = public_data(notification.data or {})
    data.update(
        notification_id=str(notification.id),
        category=notification.category.value,
        url=deep_link(notification.category, data, family_id=notification.family_id),
    )
    if notification.family_id is not None:
        data.setdefault("family_id", str(notification.family_id))
    return {
        "to": token,
        "title": notification.title,
        "body": notification.body,
        "data": data,
        "sound": "default",
        "channelId": "default",
        "priority": "high",
    }


def _chunks[T](items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def _post(
    client: httpx.AsyncClient, settings: Settings, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if settings.expo_access_token is not None:
        headers["Authorization"] = f"Bearer {settings.expo_access_token.get_secret_value()}"
    try:
        response = await client.post(settings.expo_push_url, json=messages, headers=headers)
    except httpx.HTTPError as exc:
        raise _TransientError(type(exc).__name__) from exc
    if response.status_code == 429 or response.status_code >= 500:
        raise _TransientError(f"http_{response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        if response.is_success:
            raise _RejectedError("invalid_json") from exc
        raise _RejectedError(f"http_{response.status_code}") from exc
    if not response.is_success or not isinstance(body, dict):
        codes = []
        if isinstance(body, dict) and isinstance(body.get("errors"), list):
            codes = [str(e.get("code")) for e in body["errors"] if isinstance(e, dict)]
        raise _RejectedError(f"http_{response.status_code}:{','.join(codes)}")
    tickets = body.get("data")
    if not isinstance(tickets, list) or len(tickets) != len(messages):
        raise _RejectedError("unexpected_ticket_count")
    return [t if isinstance(t, dict) else {} for t in tickets]


def _apply_outcome(
    notification: Notification, outcome: _Outcome, now: datetime, result: PushRunResult
) -> None:
    if outcome.ok:
        notification.push_status = PushStatus.SENT
        notification.sent_at = now
        result.sent += 1
    elif outcome.transient:
        data = dict(notification.data or {})
        attempts = int(data.get(PUSH_ATTEMPTS_KEY, 0) or 0) + 1
        if attempts >= MAX_TRANSIENT_ATTEMPTS and notification.scheduled_for <= now - GIVE_UP_AFTER:
            notification.push_status = PushStatus.FAILED
            result.failed += 1
            log.warning("push_gave_up", notification_id=str(notification.id), attempts=attempts)
        else:
            result.retrying += 1
        data[PUSH_ATTEMPTS_KEY] = attempts
        notification.data = data
    elif outcome.errors:
        notification.push_status = PushStatus.FAILED
        result.failed += 1
    else:  # every device it was sent to is no longer registered
        notification.push_status = PushStatus.SKIPPED
        result.skipped += 1


async def _send_batch(
    db: Database, settings: Settings, client_factory: _ClientFactory
) -> tuple[PushRunResult, bool]:
    """One claimed batch in one transaction. Returns (result, had_transient_error)."""
    result = PushRunResult()
    transient = False
    async with db.sessionmaker() as session:
        now = utcnow()
        rows = await NotificationRepository(session).claim_due_push(now, PUSH_BATCH_SIZE)
        result.claimed = len(rows)
        if not rows:
            await session.commit()
            return result, False

        if not settings.push_enabled:
            for n in rows:
                n.push_status = PushStatus.SKIPPED
            result.skipped = len(rows)
            await session.commit()
            return result, False

        user_ids = {n.user_id for n in rows}
        active_users = set(
            await session.scalars(
                select(User.id).where(
                    User.id.in_(user_ids), User.is_active.is_(True), User.deleted_at.is_(None)
                )
            )
        )
        tokens_by_user: dict[uuid.UUID, list[DeviceToken]] = defaultdict(list)
        for token, user_session in await DeviceTokenRepository(session).active_with_sessions(
            active_users
        ):
            if _session_live(user_session, now, settings):
                tokens_by_user[token.user_id].append(token)
            else:
                token.disabled_at = now
                result.devices_disabled += 1

        messages: list[_Message] = []
        outcomes: dict[uuid.UUID, _Outcome] = {}
        for n in rows:
            tokens = tokens_by_user.get(n.user_id, [])
            if not tokens:
                n.push_status = PushStatus.SKIPPED
                result.skipped += 1
                continue
            outcomes[n.id] = _Outcome()
            messages.extend(_Message(n, t, _payload(n, t.token)) for t in tokens)

        if messages:
            client = await client_factory.get()
            for chunk in _chunks(messages, EXPO_MAX_MESSAGES):
                result.requests += 1
                try:
                    tickets = await _post(client, settings, [m.payload for m in chunk])
                except _TransientError as exc:
                    transient = True
                    log.warning("push_request_transient_error", error=str(exc), size=len(chunk))
                    for m in chunk:
                        outcomes[m.notification.id].transient = True
                    continue
                except _RejectedError as exc:
                    log.error("push_request_rejected", error=str(exc), size=len(chunk))
                    for m in chunk:
                        outcomes[m.notification.id].errors += 1
                    continue
                for m, ticket in zip(chunk, tickets, strict=True):
                    outcome = outcomes[m.notification.id]
                    if ticket.get("status") == "ok":
                        outcome.ok += 1
                        continue
                    details = ticket.get("details")
                    error = details.get("error") if isinstance(details, dict) else None
                    if error == DEVICE_NOT_REGISTERED:
                        outcome.unregistered += 1
                        if m.token.disabled_at is None:
                            m.token.disabled_at = now
                            result.devices_disabled += 1
                    else:
                        outcome.errors += 1
                    # Never log the ticket "message": Expo echoes the token in it.
                    log.warning(
                        "push_ticket_error",
                        notification_id=str(m.notification.id),
                        error=str(error or "unknown"),
                        device=mask_token(m.token.token),
                    )

        for n in rows:
            if n.id in outcomes:
                _apply_outcome(n, outcomes[n.id], now, result)
        await session.commit()
    return result, transient


class _ClientFactory:
    """Creates the HTTP client only when a batch actually has something to send."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None) -> None:
        self.settings = settings
        self.transport = transport
        self.client: httpx.AsyncClient | None = None

    async def get(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(
                transport=self.transport, timeout=self.settings.expo_push_timeout_seconds
            )
        return self.client

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()


async def send_due_pushes(
    db: Database,
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    max_batches: int = MAX_BATCHES_PER_RUN,
) -> PushRunResult:
    """Deliver due pending pushes (up to ``max_batches`` full batches). ``transport`` lets
    tests substitute ``httpx.MockTransport``."""
    total = PushRunResult()
    factory = _ClientFactory(settings, transport)
    try:
        for _ in range(max_batches):
            result, transient = await _send_batch(db, settings, factory)
            total.add(result)
            # Stop on a short batch, or when Expo is struggling (the next run retries).
            if result.claimed < PUSH_BATCH_SIZE or transient:
                break
    finally:
        await factory.aclose()
    if total.claimed:
        log.info("push_run", **{k: getattr(total, k) for k in total.__dataclass_fields__})
    return total
