"""Queries for notifications, notification preferences and Expo device tokens.

Every user-facing lookup is scoped to one ``user_id``; callers never get another user's rows.

A *deleted* notification is kept as a tombstone (``data`` gets ``_deleted_at``) instead of being
removed: the row's ``(user_id, dedupe_key)`` must survive, otherwise a periodic producer (budget
alerts, bill reminders, ...) would simply re-create the notification the user just dismissed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, Select, and_, func, not_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import DevicePlatform, NotificationCategory, PushStatus
from app.models.notification import DeviceToken, Notification, NotificationPreference
from app.models.user import UserSession

# Internal keys kept in ``Notification.data`` (never returned by the API).
DELETED_KEY = "_deleted_at"
PUSH_ATTEMPTS_KEY = "_push_attempts"


def not_deleted() -> ColumnElement[bool]:
    return not_(Notification.data.bool_op("?")(DELETED_KEY))


def visible_to(user_id: uuid.UUID, now: datetime) -> ColumnElement[bool]:
    """The user's own, already-due, non-deleted notifications."""
    return and_(
        Notification.user_id == user_id,
        Notification.scheduled_for <= now,
        not_deleted(),
    )


class NotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def list_stmt(
        self,
        user_id: uuid.UUID,
        now: datetime,
        *,
        unread_only: bool = False,
        category: NotificationCategory | None = None,
    ) -> Select[Notification]:
        stmt = select(Notification).where(visible_to(user_id, now))
        if unread_only:
            stmt = stmt.where(Notification.read_at.is_(None))
        if category is not None:
            stmt = stmt.where(Notification.category == category)
        return stmt.order_by(
            Notification.scheduled_for.desc(), Notification.created_at.desc(), Notification.id
        )

    async def get_visible(
        self,
        user_id: uuid.UUID,
        notification_id: uuid.UUID,
        now: datetime,
        *,
        for_update: bool = False,
    ) -> Notification | None:
        stmt = select(Notification).where(
            Notification.id == notification_id, visible_to(user_id, now)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt)

    async def unread_count(self, user_id: uuid.UUID, now: datetime) -> int:
        stmt = select(func.count()).where(visible_to(user_id, now), Notification.read_at.is_(None))
        return int((await self.session.execute(stmt)).scalar_one())

    async def mark_all_read(
        self, user_id: uuid.UUID, now: datetime, category: NotificationCategory | None = None
    ) -> int:
        stmt = (
            update(Notification)
            .where(visible_to(user_id, now), Notification.read_at.is_(None))
            .values(read_at=now)
            .execution_options(synchronize_session=False)
        )
        if category is not None:
            stmt = stmt.where(Notification.category == category)
        result = await self.session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)

    async def skip_pending_push(
        self, user_id: uuid.UUID, categories: Sequence[NotificationCategory]
    ) -> int:
        """Pending pushes of categories whose push channel was just turned off."""
        if not categories:
            return 0
        result = await self.session.execute(
            update(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.category.in_(list(categories)),
                Notification.push_status == PushStatus.PENDING,
            )
            .values(push_status=PushStatus.SKIPPED)
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def claim_due_push(self, now: datetime, limit: int) -> list[Notification]:
        """Lock due, pending pushes; concurrent workers skip rows another worker holds."""
        return list(
            await self.session.scalars(
                select(Notification)
                .where(
                    Notification.push_status == PushStatus.PENDING,
                    Notification.scheduled_for <= now,
                )
                .order_by(Notification.scheduled_for, Notification.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )


class PreferenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for_user(self, user_id: uuid.UUID) -> list[NotificationPreference]:
        return list(
            await self.session.scalars(
                select(NotificationPreference).where(NotificationPreference.user_id == user_id)
            )
        )

    async def upsert(
        self, user_id: uuid.UUID, items: Iterable[tuple[NotificationCategory, bool, bool]]
    ) -> None:
        """``items`` = (category, push_enabled, in_app_enabled)."""
        for category, push_enabled, in_app_enabled in items:
            stmt = pg_insert(NotificationPreference).values(
                user_id=user_id,
                category=category,
                push_enabled=push_enabled,
                in_app_enabled=in_app_enabled,
            )
            await self.session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["user_id", "category"],
                    set_={
                        "push_enabled": stmt.excluded.push_enabled,
                        "in_app_enabled": stmt.excluded.in_app_enabled,
                        "updated_at": func.now(),
                    },
                )
            )


class DeviceTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_token(self, token: str) -> DeviceToken | None:
        return await self.session.scalar(select(DeviceToken).where(DeviceToken.token == token))

    async def upsert(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        token: str,
        platform: DevicePlatform,
        now: datetime,
    ) -> uuid.UUID:
        """Insert the token, or re-bind an existing row (any user) to this user/session and
        re-enable it. Atomic, so two concurrent registrations of one token cannot collide."""
        stmt = pg_insert(DeviceToken).values(
            user_id=user_id,
            session_id=session_id,
            token=token,
            platform=platform,
            last_seen_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["token"],
            set_={
                "user_id": stmt.excluded.user_id,
                "session_id": stmt.excluded.session_id,
                "platform": stmt.excluded.platform,
                "last_seen_at": stmt.excluded.last_seen_at,
                "disabled_at": None,
            },
        )
        return (await self.session.execute(stmt.returning(DeviceToken.id))).scalar_one()

    async def disable_other_tokens_of_session(
        self, session_id: uuid.UUID, keep_id: uuid.UUID, now: datetime
    ) -> int:
        """A device (session) has one current push token; older ones are retired."""
        result = await self.session.execute(
            update(DeviceToken)
            .where(
                DeviceToken.session_id == session_id,
                DeviceToken.id != keep_id,
                DeviceToken.disabled_at.is_(None),
            )
            .values(disabled_at=now)
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def disable_for_sessions(self, session_ids: Sequence[uuid.UUID], now: datetime) -> int:
        if not session_ids:
            return 0
        result = await self.session.execute(
            update(DeviceToken)
            .where(DeviceToken.session_id.in_(list(session_ids)), DeviceToken.disabled_at.is_(None))
            .values(disabled_at=now)
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def disable_for_user(
        self, user_id: uuid.UUID, now: datetime, *, except_session_id: uuid.UUID | None = None
    ) -> int:
        stmt = (
            update(DeviceToken)
            .where(DeviceToken.user_id == user_id, DeviceToken.disabled_at.is_(None))
            .values(disabled_at=now)
            .execution_options(synchronize_session=False)
        )
        if except_session_id is not None:
            # IS DISTINCT FROM: tokens with no session are disabled too.
            stmt = stmt.where(DeviceToken.session_id.is_distinct_from(except_session_id))
        result = await self.session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)

    async def active_with_sessions(
        self, user_ids: Iterable[uuid.UUID]
    ) -> list[tuple[DeviceToken, UserSession | None]]:
        """Enabled tokens of these users, each with its bound session (None if gone)."""
        ids = list(dict.fromkeys(user_ids))
        if not ids:
            return []
        rows = await self.session.execute(
            select(DeviceToken, UserSession)
            .outerjoin(UserSession, UserSession.id == DeviceToken.session_id)
            .where(DeviceToken.user_id.in_(ids), DeviceToken.disabled_at.is_(None))
            .order_by(DeviceToken.created_at, DeviceToken.id)
        )
        return [(t, s) for t, s in rows]
