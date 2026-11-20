"""Families, memberships and invitations (Phase 3)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPkMixin
from app.models.enums import FAMILY_ROLE, INVITATION_STATUS, FamilyRole, InvitationStatus
from app.models.user import User


class Family(UUIDPkMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A household. Never hard-deleted: ``deleted_at`` hides it (and all its data) everywhere."""

    __tablename__ = "families"

    name: Mapped[str] = mapped_column(String(80))
    avatar_key: Mapped[str | None] = mapped_column(String(255))
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))


class FamilyMember(UUIDPkMixin, TimestampMixin, Base):
    """One membership period. Leaving/removal sets ``left_at``; re-joining creates a new row,
    so history is preserved. *Active* = ``left_at IS NULL``."""

    __tablename__ = "family_members"
    __table_args__ = (
        # At most one active membership per (family, user)…
        Index(
            "uq_family_members_active_user",
            "family_id",
            "user_id",
            unique=True,
            postgresql_where=text("left_at IS NULL"),
        ),
        # …and at most one active owner per family (the service guarantees at least one).
        Index(
            "uq_family_members_active_owner",
            "family_id",
            unique=True,
            postgresql_where=text("role = 'owner' AND left_at IS NULL"),
        ),
        Index("ix_family_members_user_active", "user_id", postgresql_where=text("left_at IS NULL")),
    )

    family_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    role: Mapped[FamilyRole] = mapped_column(FAMILY_ROLE)
    joined_at: Mapped[datetime] = mapped_column(server_default=func.now())
    left_at: Mapped[datetime | None]
    # Set when someone else removed the member (NULL = left voluntarily / still active).
    removed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))

    user: Mapped[User] = relationship(foreign_keys=[user_id], lazy="raise")

    @property
    def is_active(self) -> bool:
        return self.left_at is None


class FamilyInvitation(UUIDPkMixin, TimestampMixin, Base):
    """An email invitation. The link token is shown once; only its SHA-256 is stored.

    ``status`` stays ``pending`` past ``expires_at`` until something touches the row, so
    "usable" = ``status = 'pending' AND expires_at > now()``.
    """

    __tablename__ = "family_invitations"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint("role <> 'owner'", name="role_not_owner"),
        Index(
            "uq_family_invitations_pending_email",
            "family_id",
            "email",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_family_invitations_email_status", "email", "status"),
    )

    family_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[FamilyRole] = mapped_column(FAMILY_ROLE)
    invited_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[InvitationStatus] = mapped_column(
        INVITATION_STATUS,
        default=InvitationStatus.PENDING,
        server_default=InvitationStatus.PENDING.value,
    )
    expires_at: Mapped[datetime]
    responded_at: Mapped[datetime | None]
    accepted_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
