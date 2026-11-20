"""Queries for families, memberships and invitations.

Every lookup here excludes soft-deleted families; authorisation itself lives in
``app.core.permissions`` / ``app.services.families``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.enums import FamilyRole, InvitationStatus
from app.models.family import Family, FamilyInvitation, FamilyMember
from app.models.user import User


class FamilyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, obj: Family | FamilyMember) -> None:
        self.session.add(obj)

    async def get_live(self, family_id: uuid.UUID, *, for_update: bool = False) -> Family | None:
        stmt = select(Family).where(Family.id == family_id, Family.deleted_at.is_(None))
        if for_update:
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt)

    async def get_active_member(
        self, family_id: uuid.UUID, user_id: uuid.UUID, *, for_update: bool = False
    ) -> FamilyMember | None:
        stmt = select(FamilyMember).where(
            FamilyMember.family_id == family_id,
            FamilyMember.user_id == user_id,
            FamilyMember.left_at.is_(None),
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt)

    async def list_members(self, family_id: uuid.UUID) -> list[tuple[FamilyMember, User]]:
        """Active members with their user rows, owner first, then by join date."""
        rows = await self.session.execute(
            select(FamilyMember, User)
            .join(User, User.id == FamilyMember.user_id)
            .where(FamilyMember.family_id == family_id, FamilyMember.left_at.is_(None))
            .order_by(
                (FamilyMember.role == FamilyRole.OWNER).desc(),
                FamilyMember.joined_at,
                FamilyMember.id,
            )
        )
        return [(m, u) for m, u in rows]

    async def count_active_members(self, family_id: uuid.UUID) -> int:
        count = await self.session.scalar(
            select(func.count())
            .select_from(FamilyMember)
            .where(FamilyMember.family_id == family_id, FamilyMember.left_at.is_(None))
        )
        return int(count or 0)

    async def list_for_user(self, user_id: uuid.UUID) -> list[tuple[Family, FamilyRole, int]]:
        """``(family, my_role, active_member_count)`` for each live family the user is in."""
        others = aliased(FamilyMember)
        member_count = (
            select(func.count())
            .select_from(others)
            .where(others.family_id == Family.id, others.left_at.is_(None))
            .scalar_subquery()
        )
        rows = await self.session.execute(
            select(Family, FamilyMember.role, member_count)
            .join(FamilyMember, FamilyMember.family_id == Family.id)
            .where(
                FamilyMember.user_id == user_id,
                FamilyMember.left_at.is_(None),
                Family.deleted_at.is_(None),
            )
            .order_by(Family.created_at, Family.id)
        )
        return [(f, role, int(count)) for f, role, count in rows]

    async def families_blocking_account_deletion(self, user_id: uuid.UUID) -> list[Family]:
        """Live families the user owns that still have other active members."""
        others = aliased(FamilyMember)
        has_others = (
            select(others.id)
            .where(
                others.family_id == Family.id,
                others.user_id != user_id,
                others.left_at.is_(None),
            )
            .exists()
        )
        result = await self.session.scalars(
            select(Family)
            .join(FamilyMember, FamilyMember.family_id == Family.id)
            .where(
                FamilyMember.user_id == user_id,
                FamilyMember.role == FamilyRole.OWNER,
                FamilyMember.left_at.is_(None),
                Family.deleted_at.is_(None),
                has_others,
            )
        )
        return list(result)

    async def active_memberships(self, user_id: uuid.UUID) -> list[FamilyMember]:
        result = await self.session.scalars(
            select(FamilyMember)
            .where(FamilyMember.user_id == user_id, FamilyMember.left_at.is_(None))
            .with_for_update()
        )
        return list(result)


class InvitationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, invitation: FamilyInvitation) -> None:
        self.session.add(invitation)

    async def get(
        self, invitation_id: uuid.UUID, *, for_update: bool = False
    ) -> FamilyInvitation | None:
        stmt = select(FamilyInvitation).where(FamilyInvitation.id == invitation_id)
        if for_update:
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt)

    async def get_by_token_hash(
        self, token_hash: str, *, for_update: bool = False
    ) -> FamilyInvitation | None:
        stmt = select(FamilyInvitation).where(FamilyInvitation.token_hash == token_hash)
        if for_update:
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt)

    async def get_pending_for_email(
        self, family_id: uuid.UUID, email: str, *, for_update: bool = False
    ) -> FamilyInvitation | None:
        stmt = select(FamilyInvitation).where(
            FamilyInvitation.family_id == family_id,
            FamilyInvitation.email == email,
            FamilyInvitation.status == InvitationStatus.PENDING,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt)

    async def count_usable(self, family_id: uuid.UUID, now: datetime) -> int:
        count = await self.session.scalar(
            select(func.count())
            .select_from(FamilyInvitation)
            .where(
                FamilyInvitation.family_id == family_id,
                FamilyInvitation.status == InvitationStatus.PENDING,
                FamilyInvitation.expires_at > now,
            )
        )
        return int(count or 0)

    async def list_usable_for_family(
        self, family_id: uuid.UUID, now: datetime
    ) -> list[FamilyInvitation]:
        result = await self.session.scalars(
            select(FamilyInvitation)
            .where(
                FamilyInvitation.family_id == family_id,
                FamilyInvitation.status == InvitationStatus.PENDING,
                FamilyInvitation.expires_at > now,
            )
            .order_by(FamilyInvitation.created_at.desc(), FamilyInvitation.id)
        )
        return list(result)

    async def list_incoming(
        self, email: str, now: datetime
    ) -> list[tuple[FamilyInvitation, Family, User]]:
        """Usable invitations addressed to ``email`` in live families, with family + inviter."""
        rows = await self.session.execute(
            select(FamilyInvitation, Family, User)
            .join(Family, Family.id == FamilyInvitation.family_id)
            .join(User, User.id == FamilyInvitation.invited_by_user_id)
            .where(
                FamilyInvitation.email == email,
                FamilyInvitation.status == InvitationStatus.PENDING,
                FamilyInvitation.expires_at > now,
                Family.deleted_at.is_(None),
            )
            .order_by(FamilyInvitation.created_at.desc(), FamilyInvitation.id)
        )
        return [(i, f, u) for i, f, u in rows]

    async def revoke_all_pending(self, family_id: uuid.UUID, now: datetime) -> int:
        result = await self.session.execute(
            update(FamilyInvitation)
            .where(
                FamilyInvitation.family_id == family_id,
                FamilyInvitation.status == InvitationStatus.PENDING,
            )
            .values(status=InvitationStatus.REVOKED, responded_at=now)
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)
