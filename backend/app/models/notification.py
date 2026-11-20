"""In-app notifications, per-category preferences and Expo push tokens (Phase 7)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPkMixin
from app.models.enums import (
    DEVICE_PLATFORM,
    NOTIFICATION_CATEGORY,
    PUSH_STATUS,
    DevicePlatform,
    NotificationCategory,
    PushStatus,
)


class Notification(UUIDPkMixin, CreatedAtMixin, Base):
    """One notification for one user. ``dedupe_key`` (unique per user, e.g.
    ``budget:<id>:2026-11:75``) makes producers idempotent."""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("user_id", "dedupe_key"),
        Index("ix_notifications_user_created", "user_id", "created_at"),
        Index("ix_notifications_user_unread", "user_id", postgresql_where=text("read_at IS NULL")),
        Index(
            "ix_notifications_push_due",
            "scheduled_for",
            postgresql_where=text("push_status = 'pending'"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    family_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("families.id", ondelete="CASCADE")
    )
    category: Mapped[NotificationCategory] = mapped_column(NOTIFICATION_CATEGORY)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    read_at: Mapped[datetime | None]
    scheduled_for: Mapped[datetime] = mapped_column(server_default=func.now())
    sent_at: Mapped[datetime | None]
    push_status: Mapped[PushStatus] = mapped_column(
        PUSH_STATUS, default=PushStatus.PENDING, server_default=PushStatus.PENDING.value
    )


class NotificationPreference(TimestampMixin, Base):
    """Missing row = defaults (both channels enabled)."""

    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    category: Mapped[NotificationCategory] = mapped_column(NOTIFICATION_CATEGORY, primary_key=True)
    push_enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    in_app_enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))


class DeviceToken(UUIDPkMixin, CreatedAtMixin, Base):
    """Expo push token. ``disabled_at`` is set when Expo reports it invalid
    (``DeviceNotRegistered``) or the bound session is revoked."""

    __tablename__ = "device_tokens"
    __table_args__ = (
        Index(
            "ix_device_tokens_user_active", "user_id", postgresql_where=text("disabled_at IS NULL")
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_sessions.id", ondelete="SET NULL")
    )
    token: Mapped[str] = mapped_column(String(255), unique=True)
    platform: Mapped[DevicePlatform] = mapped_column(DEVICE_PLATFORM)
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    disabled_at: Mapped[datetime | None]
