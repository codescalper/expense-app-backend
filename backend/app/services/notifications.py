"""Notification inbox, per-category preferences, Expo device tokens, and spending insights.

* A user only ever sees their own notifications, and only once ``scheduled_for`` has passed;
  anything else is 404.
* Deleting a notification keeps a tombstone (see ``repositories/notifications.py``) so
  producers' dedupe keys keep working.
* Device tokens are bound to the session that registered them. When that session ends the
  token must stop receiving pushes: auth code calls :func:`disable_tokens_for_sessions` /
  :func:`disable_tokens_for_user`, and the push sender also disables any token whose session
  turns out to be dead.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.security import utcnow
from app.models.enums import NotificationCategory, PushStatus, TransactionType
from app.models.finance import Transaction
from app.models.notification import Notification
from app.models.user import User
from app.repositories.notifications import (
    DELETED_KEY,
    DeviceTokenRepository,
    NotificationRepository,
    PreferenceRepository,
)
from app.schemas.notifications import (
    DeviceOut,
    DeviceRegister,
    NotificationOut,
    PreferenceOut,
    PreferenceUpdate,
)
from app.services import ledger
from app.services.notify import notify
from app.services.push import deep_link, mask_token, public_data
from app.utils.pagination import Page, PageParams, paginate


def notification_out(n: Notification) -> NotificationOut:
    data = public_data(n.data or {})
    return NotificationOut(
        id=n.id,
        category=n.category,
        title=n.title,
        body=n.body,
        data=data,
        url=deep_link(n.category, data, family_id=n.family_id),
        family_id=n.family_id,
        read=n.read_at is not None,
        read_at=n.read_at,
        created_at=n.created_at,
        scheduled_for=n.scheduled_for,
        push_status=n.push_status,
    )


class NotificationService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.db = session
        self.settings = settings
        self.notifications = NotificationRepository(session)
        self.preferences = PreferenceRepository(session)
        self.devices = DeviceTokenRepository(session)

    # ------------------------------------------------------------------ inbox

    async def list_notifications(
        self,
        user: User,
        page: PageParams,
        *,
        unread_only: bool = False,
        category: NotificationCategory | None = None,
    ) -> Page[NotificationOut]:
        stmt = self.notifications.list_stmt(
            user.id, utcnow(), unread_only=unread_only, category=category
        )
        rows, total = await paginate(self.db, stmt, page)
        return Page.build([notification_out(n) for n in rows], total, page)

    async def unread_count(self, user: User) -> int:
        return await self.notifications.unread_count(user.id, utcnow())

    async def _get(self, user: User, notification_id: uuid.UUID) -> Notification:
        n = await self.notifications.get_visible(
            user.id, notification_id, utcnow(), for_update=True
        )
        if n is None:
            raise NotFoundError("Notification not found")
        return n

    async def mark_read(self, user: User, notification_id: uuid.UUID) -> NotificationOut:
        n = await self._get(user, notification_id)
        if n.read_at is None:
            n.read_at = utcnow()
            await self.db.commit()
        return notification_out(n)

    async def mark_all_read(self, user: User, category: NotificationCategory | None = None) -> int:
        updated = await self.notifications.mark_all_read(user.id, utcnow(), category)
        await self.db.commit()
        return updated

    async def delete(self, user: User, notification_id: uuid.UUID) -> None:
        n = await self._get(user, notification_id)
        now = utcnow()
        n.data = {**(n.data or {}), DELETED_KEY: now.isoformat()}
        n.read_at = n.read_at or now
        if n.push_status is PushStatus.PENDING:
            n.push_status = PushStatus.SKIPPED
        await self.db.commit()

    # ------------------------------------------------------------------ preferences

    async def get_preferences(self, user: User) -> list[PreferenceOut]:
        stored = {p.category: p for p in await self.preferences.list_for_user(user.id)}
        out = []
        for category in NotificationCategory:
            pref = stored.get(category)
            out.append(
                PreferenceOut(
                    category=category,
                    push_enabled=pref.push_enabled if pref else True,
                    in_app_enabled=pref.in_app_enabled if pref else True,
                )
            )
        return out

    async def update_preferences(
        self, user: User, items: list[PreferenceUpdate]
    ) -> list[PreferenceOut]:
        categories = [i.category for i in items]
        if len(set(categories)) != len(categories):
            raise ValidationFailedError(
                "Each category may appear only once", code="duplicate_category"
            )
        await self.preferences.upsert(
            user.id, [(i.category, i.push_enabled, i.in_app_enabled) for i in items]
        )
        # Pushes already queued for categories just muted are not sent any more.
        await self.notifications.skip_pending_push(
            user.id, [i.category for i in items if not i.push_enabled]
        )
        await self.db.commit()
        return await self.get_preferences(user)

    # ------------------------------------------------------------------ devices

    async def register_device(
        self, user: User, session_id: uuid.UUID, data: DeviceRegister
    ) -> DeviceOut:
        """Upsert by token: a token already known (for this or another user — e.g. the phone
        changed hands or someone signed in on it) is re-bound to the caller's current session
        and re-enabled. Any other token of the same session is retired."""
        now = utcnow()
        token_id = await self.devices.upsert(
            user_id=user.id,
            session_id=session_id,
            token=data.token,
            platform=data.platform,
            now=now,
        )
        await self.devices.disable_other_tokens_of_session(session_id, token_id, now)
        await self.db.commit()
        device = await self.devices.get_by_token(data.token)
        assert device is not None  # noqa: S101 - just upserted in this transaction
        await self.db.refresh(device)
        return DeviceOut(
            id=device.id,
            platform=device.platform,
            token_hint=mask_token(device.token),
            created_at=device.created_at,
            last_seen_at=device.last_seen_at,
        )

    async def unregister_device(self, user: User, token: str) -> None:
        device = await self.devices.get_by_token(token)
        if device is None or device.user_id != user.id:
            raise NotFoundError("Device not found")
        await self.db.delete(device)
        await self.db.commit()


# --- hooks for the auth/session code ---------------------------------------------------------


async def disable_tokens_for_sessions(
    session: AsyncSession, session_ids: list[uuid.UUID], now: datetime | None = None
) -> int:
    """Stop pushes to devices of ended sessions (logout / session revoke). Does **not**
    commit — call inside the transaction that revokes the sessions."""
    return await DeviceTokenRepository(session).disable_for_sessions(session_ids, now or utcnow())


async def disable_tokens_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    except_session_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> int:
    """Disable all of a user's device tokens (logout-all, password reset/change, account
    deletion), optionally keeping the current session's. Does **not** commit."""
    return await DeviceTokenRepository(session).disable_for_user(
        user_id, now or utcnow(), except_session_id=except_session_id
    )


# --- insight: unusual spending ----------------------------------------------------------------
#
# Rule (spec §11 "unusual spending"), evaluated hourly by workers/jobs/insights.py:
#   today      = the user's current calendar day in their own time zone
#   spent      = sum of ledger.expense_amount (expenses - refunds) over ledger.counted rows the
#                user recorded themselves (owner_user_id; personal *and* family scope), today
#   baseline   = same sum over the 28 previous local days / 28 (days with no spending count as 0)
#   alert when spent > max(3 x baseline, ₹1,000)
# Amounts are compared per currency (the ₹1,000 floor is 1,000 units of that currency); at most
# one alert per user per local day (dedupe key ``unusual:<user_id>:<YYYY-MM-DD>``).

UNUSUAL_MULTIPLIER = Decimal(3)
UNUSUAL_MIN_AMOUNT = Decimal(1000)
BASELINE_DAYS = 28
DEFAULT_TIMEZONE = "Asia/Kolkata"
_SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def _money(amount: Decimal, currency: str) -> str:
    symbol = _SYMBOLS.get(currency)
    text = f"{amount.quantize(Decimal('1')):,}"
    return f"{symbol}{text}" if symbol else f"{currency} {text}"


async def check_unusual_spending(session: AsyncSession, user: User, now: datetime) -> bool:
    """Apply the rule above to one user. Returns True if a notification was created. Does
    **not** commit."""
    tz = _zone(user.timezone)
    today = now.astimezone(tz).date()
    day_start = datetime.combine(today, time.min, tz)
    day_end = datetime.combine(today + timedelta(days=1), time.min, tz)
    window_start = datetime.combine(today - timedelta(days=BASELINE_DAYS), time.min, tz)
    spend = ledger.expense_amount()
    today_sum = func.coalesce(
        func.sum(case((Transaction.occurred_at >= day_start, spend), else_=0)), 0
    )
    baseline_sum = func.coalesce(
        func.sum(case((Transaction.occurred_at < day_start, spend), else_=0)), 0
    )
    rows = await session.execute(
        select(Transaction.currency, today_sum, baseline_sum)
        .where(
            ledger.counted(),
            ledger.is_spending(),
            Transaction.owner_user_id == user.id,
            Transaction.occurred_at >= window_start,
            Transaction.occurred_at < day_end,
        )
        .group_by(Transaction.currency)
        .order_by(Transaction.currency)
    )
    for currency, spent_raw, baseline_raw in rows:
        spent = Decimal(spent_raw).quantize(Decimal("0.01"))
        average = (Decimal(baseline_raw) / BASELINE_DAYS).quantize(Decimal("0.01"))
        threshold = max(UNUSUAL_MULTIPLIER * average, UNUSUAL_MIN_AMOUNT)
        if spent <= threshold:
            continue
        body = f"You've spent {_money(spent, currency)} today"
        if average > 0:
            body += f", well above your daily average of {_money(average, currency)}"
        return await notify(
            session,
            user_id=user.id,
            category=NotificationCategory.UNUSUAL_SPENDING,
            title="Unusual spending today",
            body=body + ".",
            dedupe_key=f"unusual:{user.id}:{today.isoformat()}",
            data={
                "date": today.isoformat(),
                "currency": currency,
                "amount": str(spent),
                "daily_average": str(average),
            },
        )
    return False


async def run_unusual_spending_check(session: AsyncSession, now: datetime | None = None) -> int:
    """Check every active user who recorded spending in the last two days (covers "today" in
    every time zone). Idempotent: re-runs on the same local day create nothing new. Does
    **not** commit. Returns notifications created."""
    now = now or utcnow()
    candidates = await session.scalars(
        select(User)
        .where(
            User.is_active.is_(True),
            User.deleted_at.is_(None),
            User.id.in_(
                select(Transaction.owner_user_id).where(
                    ledger.counted(),
                    Transaction.type == TransactionType.EXPENSE,
                    Transaction.occurred_at >= now - timedelta(days=2),
                )
            ),
        )
        .order_by(User.id)
    )
    created = 0
    for user in list(candidates):
        if await check_unusual_spending(session, user, now):
            created += 1
    return created
