from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, SoftDeleteMixin, TimestampMixin, UUIDPkMixin


class User(UUIDPkMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        Index("uq_users_email", "email", unique=True),
    )

    email: Mapped[str] = mapped_column(String(320))
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(120))
    avatar_key: Mapped[str | None] = mapped_column(String(255))
    timezone: Mapped[str] = mapped_column(
        String(64), default="Asia/Kolkata", server_default="Asia/Kolkata"
    )
    email_verified_at: Mapped[datetime | None]
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    failed_login_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    locked_until: Mapped[datetime | None]
    password_changed_at: Mapped[datetime | None]

    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def is_email_verified(self) -> bool:
        return self.email_verified_at is not None


class UserSession(UUIDPkMixin, CreatedAtMixin, Base):
    """One logged-in device. Its refresh tokens rotate; the session id is the JWT ``sid``."""

    __tablename__ = "user_sessions"
    __table_args__ = (Index("ix_user_sessions_user_active", "user_id", "revoked_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    device_name: Mapped[str | None] = mapped_column(String(100))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    last_used_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    revoked_reason: Mapped[str | None] = mapped_column(String(40))

    user: Mapped[User] = relationship(back_populates="sessions")


class RefreshToken(UUIDPkMixin, Base):
    """Opaque refresh token (only its SHA-256 is stored). ``rotated_at`` set = already used."""

    __tablename__ = "refresh_tokens"

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_sessions.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    issued_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    rotated_at: Mapped[datetime | None]

    session: Mapped[UserSession] = relationship()


class TokenPurpose(enum.StrEnum):
    EMAIL_VERIFY = "email_verify"
    PASSWORD_RESET = "password_reset"  # noqa: S105 - enum label, not a secret


class UserToken(UUIDPkMixin, CreatedAtMixin, Base):
    """Short-lived one-time email codes (verification / password reset), stored as HMACs."""

    __tablename__ = "user_tokens"
    __table_args__ = (Index("ix_user_tokens_user_purpose", "user_id", "purpose"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    purpose: Mapped[TokenPurpose] = mapped_column(
        Enum(TokenPurpose, name="token_purpose", values_callable=lambda e: [m.value for m in e])
    )
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime]
    consumed_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(default=0, server_default=text("0"))
