"""Family management: families, memberships, invitations, roles and ownership (Phase 3).

All authorisation goes through ``app.core.permissions``: a caller who is not an active member
of a live family gets 404 for everything about it; a member whose role is too low gets 403.
Every membership change is written to ``audit_logs`` with ``family_id`` set.

Invitation links: the raw token is returned once to the inviter and emailed to the invitee;
only its SHA-256 is stored. A link token is a single-use bearer secret — any signed-in user
holding it may accept (so a link can be shared over chat). Accepting/rejecting *by id* is only
possible for the user whose verified email the invitation was addressed to.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import NoReturn

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    AppError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
)
from app.core.permissions import (
    FamilyPermission,
    can_manage_member,
    require_family_access,
)
from app.core.rate_limit import RateLimiter
from app.core.security import hash_token, utcnow
from app.core.threads import run_limited
from app.models.enums import FamilyRole, InvitationStatus, NotificationCategory
from app.models.family import Family, FamilyInvitation, FamilyMember
from app.models.user import User
from app.repositories.families import FamilyRepository, InvitationRepository
from app.repositories.system import AuditRepository, JobRepository
from app.repositories.users import UserRepository
from app.schemas.family import (
    FamilyCreate,
    FamilyDetailOut,
    FamilySummaryOut,
    FamilyUpdate,
    IncomingInvitationOut,
    InvitationCreate,
    InvitationCreatedOut,
    InvitationOut,
    InvitationPreviewOut,
    MemberOut,
)
from app.services.auth import ClientInfo
from app.services.email import SEND_EMAIL_JOB
from app.services.family_emails import family_invitation_email, invitation_deep_link
from app.services.notify import notify, notify_family_members
from app.services.storage import LocalFileStorage, process_avatar

INVITATION_TTL = timedelta(days=7)
INVITE_RATE_LIMIT = 20  # invitations created per user per window
INVITE_RATE_WINDOW_SECONDS = 3600
TOKEN_RATE_LIMIT = 30  # token preview/accept/reject calls per user per window
TOKEN_RATE_WINDOW_SECONDS = 900
MAX_USABLE_INVITATIONS_PER_FAMILY = 50
FAMILY_IMAGE_FOLDER = "families"


class InvitationExpiredError(AppError):
    status_code = 410
    code = "invitation_expired"
    message = "This invitation has expired"


class FamilyService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.db = session
        self.settings = settings
        self.families = FamilyRepository(session)
        self.invitations = InvitationRepository(session)
        self.users = UserRepository(session)
        self.audit = AuditRepository(session)
        self.jobs = JobRepository(session)
        # Shares the request's connection/transaction (no second pooled connection).
        self.limiter = RateLimiter(session)
        self.storage = LocalFileStorage(settings.upload_dir)

    # ------------------------------------------------------------------ helpers

    def _audit(
        self,
        action: str,
        user: User,
        client: ClientInfo,
        *,
        family_id: uuid.UUID,
        entity_type: str,
        entity_id: uuid.UUID,
        **details: object,
    ) -> None:
        self.audit.record(
            action,
            actor_user_id=user.id,
            entity_type=entity_type,
            entity_id=entity_id,
            family_id=family_id,
            ip_address=client.ip,
            user_agent=client.user_agent,
            details=dict(details),
        )

    async def _rate_limit(self, key: str, limit: int, window: int) -> None:
        if self.settings.rate_limit_enabled:
            await self.limiter.hit(key, limit, window)

    async def _live_family(self, family_id: uuid.UUID, *, for_update: bool = False) -> Family:
        family = await self.families.get_live(family_id, for_update=for_update)
        if family is None:  # pragma: no cover - require_family_access already checked
            raise NotFoundError("Family not found")
        return family

    async def _detail(self, family: Family, my_role: FamilyRole) -> FamilyDetailOut:
        members = await self.families.list_members(family.id)
        return FamilyDetailOut(
            id=family.id,
            name=family.name,
            currency=family.currency,
            has_image=family.avatar_key is not None,
            my_role=my_role,
            created_by_user_id=family.created_by_user_id,
            created_at=family.created_at,
            updated_at=family.updated_at,
            members=[
                MemberOut(
                    user_id=u.id,
                    full_name=u.full_name,
                    email=u.email,
                    role=m.role,
                    has_avatar=u.avatar_key is not None,
                    joined_at=m.joined_at,
                )
                for m, u in members
            ],
        )

    async def _summary(self, family: Family, my_role: FamilyRole) -> FamilySummaryOut:
        return FamilySummaryOut(
            id=family.id,
            name=family.name,
            currency=family.currency,
            has_image=family.avatar_key is not None,
            my_role=my_role,
            member_count=await self.families.count_active_members(family.id),
            created_at=family.created_at,
        )

    # ------------------------------------------------------------------ families

    async def create_family(
        self, user: User, data: FamilyCreate, client: ClientInfo
    ) -> FamilyDetailOut:
        family = Family(name=data.name, currency=data.currency, created_by_user_id=user.id)
        self.families.add(family)
        await self.db.flush()
        owner = FamilyMember(
            family_id=family.id, user_id=user.id, role=FamilyRole.OWNER, joined_at=utcnow()
        )
        self.families.add(owner)
        self._audit(
            "family.created",
            user,
            client,
            family_id=family.id,
            entity_type="family",
            entity_id=family.id,
        )
        await self.db.commit()
        await self.db.refresh(family)
        return await self._detail(family, FamilyRole.OWNER)

    async def list_families(self, user: User) -> list[FamilySummaryOut]:
        return [
            FamilySummaryOut(
                id=f.id,
                name=f.name,
                currency=f.currency,
                has_image=f.avatar_key is not None,
                my_role=role,
                member_count=count,
                created_at=f.created_at,
            )
            for f, role, count in await self.families.list_for_user(user.id)
        ]

    async def get_family(self, user: User, family_id: uuid.UUID) -> FamilyDetailOut:
        member = await require_family_access(self.db, user, family_id)
        return await self._detail(await self._live_family(family_id), member.role)

    async def update_family(
        self, user: User, family_id: uuid.UUID, data: FamilyUpdate, client: ClientInfo
    ) -> FamilyDetailOut:
        member = await require_family_access(
            self.db, user, family_id, FamilyPermission.UPDATE_FAMILY
        )
        family = await self._live_family(family_id, for_update=True)
        changes = data.model_dump(exclude_unset=True, exclude_none=True)
        for field, value in changes.items():
            setattr(family, field, value)
        if changes:
            self._audit(
                "family.updated",
                user,
                client,
                family_id=family.id,
                entity_type="family",
                entity_id=family.id,
                fields=sorted(changes),
            )
            await self.db.commit()
            await self.db.refresh(family)
        return await self._detail(family, member.role)

    async def delete_family(self, user: User, family_id: uuid.UUID, client: ClientInfo) -> None:
        """Owner only. Soft delete: the family and all its data disappear for every member."""
        await require_family_access(self.db, user, family_id, FamilyPermission.DELETE_FAMILY)
        family = await self._live_family(family_id, for_update=True)
        now = utcnow()
        family.deleted_at = now
        revoked = await self.invitations.revoke_all_pending(family.id, now)
        self._audit(
            "family.deleted",
            user,
            client,
            family_id=family.id,
            entity_type="family",
            entity_id=family.id,
            invitations_revoked=revoked,
        )
        await self.db.commit()

    # ------------------------------------------------------------------ image

    async def set_image(
        self, user: User, family_id: uuid.UUID, raw: bytes, client: ClientInfo
    ) -> FamilyDetailOut:
        member = await require_family_access(
            self.db, user, family_id, FamilyPermission.UPDATE_FAMILY
        )
        image = await run_limited("image", 2, process_avatar, raw, self.settings.max_avatar_bytes)
        family = await self._live_family(family_id, for_update=True)
        old_key = family.avatar_key
        family.avatar_key = self.storage.save(FAMILY_IMAGE_FOLDER, image, "jpg")
        self._audit(
            "family.image_updated",
            user,
            client,
            family_id=family.id,
            entity_type="family",
            entity_id=family.id,
        )
        await self.db.commit()
        await self.db.refresh(family)
        if old_key:
            self.storage.delete(old_key)
        return await self._detail(family, member.role)

    async def remove_image(self, user: User, family_id: uuid.UUID, client: ClientInfo) -> None:
        await require_family_access(self.db, user, family_id, FamilyPermission.UPDATE_FAMILY)
        family = await self._live_family(family_id, for_update=True)
        if family.avatar_key:
            old_key = family.avatar_key
            family.avatar_key = None
            self._audit(
                "family.image_removed",
                user,
                client,
                family_id=family.id,
                entity_type="family",
                entity_id=family.id,
            )
            await self.db.commit()
            self.storage.delete(old_key)

    async def image_path(self, user: User, family_id: uuid.UUID) -> Path:
        await require_family_access(self.db, user, family_id)
        family = await self._live_family(family_id)
        path = self.storage.path_for(family.avatar_key) if family.avatar_key else None
        if path is None:
            raise NotFoundError("No family image")
        return path

    # ------------------------------------------------------------------ invitations (admin side)

    async def create_invitation(
        self, user: User, family_id: uuid.UUID, data: InvitationCreate, client: ClientInfo
    ) -> InvitationCreatedOut:
        await require_family_access(self.db, user, family_id, FamilyPermission.INVITE_MEMBERS)
        await self._rate_limit(
            f"family_invite:{user.id}", INVITE_RATE_LIMIT, INVITE_RATE_WINDOW_SECONDS
        )
        family = await self._live_family(family_id)
        email = data.email  # already lower-cased by NormalizedEmail
        role = FamilyRole(data.role)
        now = utcnow()

        invitee = await self.users.get_by_email(email)
        if invitee is not None and await self.families.get_active_member(family_id, invitee.id):
            raise ConflictError("This person is already a member", code="already_member")

        # Re-inviting the same address replaces the previous pending invitation (new token).
        previous = await self.invitations.get_pending_for_email(family_id, email, for_update=True)
        if previous is not None:
            previous.status = InvitationStatus.REVOKED
            previous.responded_at = now
            await self.db.flush()
        if await self.invitations.count_usable(family_id, now) >= (
            MAX_USABLE_INVITATIONS_PER_FAMILY
        ):
            raise ConflictError("Too many pending invitations", code="too_many_invitations")

        token = secrets.token_urlsafe(32)
        invitation = FamilyInvitation(
            family_id=family_id,
            email=email,
            role=role,
            invited_by_user_id=user.id,
            token_hash=hash_token(token),
            status=InvitationStatus.PENDING,
            expires_at=now + INVITATION_TTL,
        )
        self.invitations.add(invitation)
        await self.db.flush()
        message = family_invitation_email(
            to=email,
            inviter_name=user.full_name,
            family_name=family.name,
            role=role.value,
            token=token,
            expires_days=INVITATION_TTL.days,
        )
        await self.jobs.enqueue(SEND_EMAIL_JOB, message.to_payload(invitation.expires_at))
        if invitee is not None and invitee.is_email_verified and invitee.is_active:
            # In-app/push copy for an existing account (by-id accept needs a verified email).
            await notify(
                self.db,
                user_id=invitee.id,
                category=NotificationCategory.FAMILY_INVITATION,
                title="Family invitation",
                body=f'{user.full_name} invited you to join "{family.name}" as {role.value}.',
                dedupe_key=f"invite:{invitation.id}",
                data={"invitation_id": str(invitation.id), "family_id": str(family_id)},
            )
        self._audit(
            "family.invitation_created",
            user,
            client,
            family_id=family_id,
            entity_type="family_invitation",
            entity_id=invitation.id,
            role=role.value,
            replaced_invitation_id=str(previous.id) if previous else None,
        )
        await self.db.commit()
        await self.db.refresh(invitation)
        return InvitationCreatedOut(
            **InvitationOut.model_validate(invitation).model_dump(),
            token=token,
            deep_link=invitation_deep_link(token),
        )

    async def list_invitations(self, user: User, family_id: uuid.UUID) -> list[InvitationOut]:
        await require_family_access(self.db, user, family_id, FamilyPermission.INVITE_MEMBERS)
        rows = await self.invitations.list_usable_for_family(family_id, utcnow())
        return [InvitationOut.model_validate(i) for i in rows]

    async def revoke_invitation(
        self, user: User, family_id: uuid.UUID, invitation_id: uuid.UUID, client: ClientInfo
    ) -> None:
        await require_family_access(self.db, user, family_id, FamilyPermission.INVITE_MEMBERS)
        invitation = await self.invitations.get(invitation_id, for_update=True)
        if invitation is None or invitation.family_id != family_id:
            raise NotFoundError("Invitation not found")
        if invitation.status is not InvitationStatus.PENDING:
            raise ConflictError(
                f"Invitation is already {invitation.status.value}", code="invitation_not_pending"
            )
        invitation.status = InvitationStatus.REVOKED
        invitation.responded_at = utcnow()
        self._audit(
            "family.invitation_revoked",
            user,
            client,
            family_id=family_id,
            entity_type="family_invitation",
            entity_id=invitation.id,
        )
        await self.db.commit()

    # ------------------------------------------------------------------ invitations (invitee side)

    async def list_incoming(self, user: User) -> list[IncomingInvitationOut]:
        if not user.is_email_verified:
            return []
        rows = await self.invitations.list_incoming(user.email, utcnow())
        return [
            IncomingInvitationOut(
                id=i.id,
                family_id=f.id,
                family_name=f.name,
                role=i.role,
                invited_by_name=inviter.full_name,
                expires_at=i.expires_at,
                created_at=i.created_at,
            )
            for i, f, inviter in rows
        ]

    async def _addressed_to(self, user: User, invitation_id: uuid.UUID) -> FamilyInvitation:
        invitation = await self.invitations.get(invitation_id, for_update=True)
        if (
            invitation is None
            or not user.is_email_verified
            or invitation.email != user.email.lower()
        ):
            raise NotFoundError("Invitation not found")
        return invitation

    async def _by_token(self, user: User, token: str) -> FamilyInvitation:
        await self._rate_limit(
            f"family_invite_token:{user.id}", TOKEN_RATE_LIMIT, TOKEN_RATE_WINDOW_SECONDS
        )
        invitation = await self.invitations.get_by_token_hash(hash_token(token), for_update=True)
        if invitation is None:
            raise NotFoundError("Invitation not found")
        return invitation

    async def _ensure_usable(self, invitation: FamilyInvitation) -> Family:
        family = await self.families.get_live(invitation.family_id)
        if family is None:
            raise NotFoundError("Invitation not found")
        if invitation.status is InvitationStatus.PENDING and invitation.expires_at <= utcnow():
            invitation.status = InvitationStatus.EXPIRED
            await self.db.commit()
        if invitation.status is InvitationStatus.EXPIRED:
            raise InvitationExpiredError()
        if invitation.status is not InvitationStatus.PENDING:
            raise ConflictError(
                f"Invitation was already {invitation.status.value}",
                code="invitation_not_pending",
            )
        return family

    async def preview_by_token(self, user: User, token: str) -> InvitationPreviewOut:
        invitation = await self._by_token(user, token)
        family = await self.families.get_live(invitation.family_id)
        if family is None:
            raise NotFoundError("Invitation not found")
        inviter = await self.users.get(invitation.invited_by_user_id)
        status = invitation.status
        if status is InvitationStatus.PENDING and invitation.expires_at <= utcnow():
            status = InvitationStatus.EXPIRED
        member = await self.families.get_active_member(family.id, user.id)
        return InvitationPreviewOut(
            id=invitation.id,
            family_id=family.id,
            family_name=family.name,
            role=invitation.role,
            invited_by_name=inviter.full_name if inviter else "Deleted user",
            status=status,
            expires_at=invitation.expires_at,
            already_member=member is not None,
        )

    async def accept_invitation(
        self, user: User, invitation_id: uuid.UUID, client: ClientInfo
    ) -> FamilySummaryOut:
        invitation = await self._addressed_to(user, invitation_id)
        return await self._accept(user, invitation, client, via="id")

    async def accept_by_token(self, user: User, token: str, client: ClientInfo) -> FamilySummaryOut:
        invitation = await self._by_token(user, token)
        return await self._accept(user, invitation, client, via="token")

    async def _accept(
        self, user: User, invitation: FamilyInvitation, client: ClientInfo, *, via: str
    ) -> FamilySummaryOut:
        family = await self._ensure_usable(invitation)
        if await self.families.get_active_member(family.id, user.id):
            raise ConflictError("You are already a member of this family", code="already_member")
        now = utcnow()
        member = FamilyMember(
            family_id=family.id, user_id=user.id, role=invitation.role, joined_at=now
        )
        self.families.add(member)
        invitation.status = InvitationStatus.ACCEPTED
        invitation.responded_at = now
        invitation.accepted_user_id = user.id
        await self.db.flush()
        await notify_family_members(
            self.db,
            family_id=family.id,
            category=NotificationCategory.FAMILY_MEMBER_JOINED,
            title="New family member",
            body=f'{user.full_name} joined "{family.name}".',
            dedupe_key=f"joined:{family.id}:{user.id}:{member.id}",
            exclude_user_id=user.id,
            data={"family_id": str(family.id), "user_id": str(user.id)},
        )
        self._audit(
            "family.member_joined",
            user,
            client,
            family_id=family.id,
            entity_type="family_member",
            entity_id=member.id,
            invitation_id=str(invitation.id),
            role=invitation.role.value,
            via=via,
            email_matched=invitation.email == user.email.lower(),
        )
        try:
            await self.db.commit()
        except IntegrityError as exc:  # concurrent accept by the same user
            await self.db.rollback()
            raise ConflictError(
                "You are already a member of this family", code="already_member"
            ) from exc
        return await self._summary(family, member.role)

    async def reject_invitation(
        self, user: User, invitation_id: uuid.UUID, client: ClientInfo
    ) -> None:
        invitation = await self._addressed_to(user, invitation_id)
        await self._reject(user, invitation, client, via="id")

    async def reject_by_token(self, user: User, token: str, client: ClientInfo) -> None:
        invitation = await self._by_token(user, token)
        await self._reject(user, invitation, client, via="token")

    async def _reject(
        self, user: User, invitation: FamilyInvitation, client: ClientInfo, *, via: str
    ) -> None:
        family = await self._ensure_usable(invitation)
        invitation.status = InvitationStatus.REJECTED
        invitation.responded_at = utcnow()
        self._audit(
            "family.invitation_rejected",
            user,
            client,
            family_id=family.id,
            entity_type="family_invitation",
            entity_id=invitation.id,
            via=via,
        )
        await self.db.commit()

    # ------------------------------------------------------------------ members

    async def change_member_role(
        self,
        user: User,
        family_id: uuid.UUID,
        target_user_id: uuid.UUID,
        new_role: FamilyRole,
        client: ClientInfo,
    ) -> FamilyDetailOut:
        actor = await require_family_access(
            self.db, user, family_id, FamilyPermission.MANAGE_MEMBERS, for_update=True
        )
        target = await self.families.get_active_member(family_id, target_user_id, for_update=True)
        if target is None:
            raise NotFoundError("Member not found")
        if not can_manage_member(actor.role, target.role, new_role):
            raise_forbidden_member_change(target.role)
        old_role = target.role
        if old_role is not new_role:
            target.role = new_role
            self._audit(
                "family.member_role_changed",
                user,
                client,
                family_id=family_id,
                entity_type="family_member",
                entity_id=target.id,
                user_id=str(target_user_id),
                old_role=old_role.value,
                new_role=new_role.value,
            )
            await self.db.commit()
        return await self._detail(await self._live_family(family_id), actor.role)

    async def remove_member(
        self, user: User, family_id: uuid.UUID, target_user_id: uuid.UUID, client: ClientInfo
    ) -> None:
        actor = await require_family_access(
            self.db, user, family_id, FamilyPermission.MANAGE_MEMBERS, for_update=True
        )
        if target_user_id == user.id:
            raise BadRequestError("Use 'leave family' to remove yourself", code="use_leave")
        target = await self.families.get_active_member(family_id, target_user_id, for_update=True)
        if target is None:
            raise NotFoundError("Member not found")
        if not can_manage_member(actor.role, target.role):
            raise_forbidden_member_change(target.role)
        target.left_at = utcnow()
        target.removed_by_user_id = user.id
        self._audit(
            "family.member_removed",
            user,
            client,
            family_id=family_id,
            entity_type="family_member",
            entity_id=target.id,
            user_id=str(target_user_id),
            role=target.role.value,
        )
        await self.db.commit()

    async def transfer_ownership(
        self, user: User, family_id: uuid.UUID, new_owner_id: uuid.UUID, client: ClientInfo
    ) -> FamilyDetailOut:
        """Atomic: the current owner becomes admin and the target becomes owner."""
        current = await require_family_access(
            self.db, user, family_id, FamilyPermission.TRANSFER_OWNERSHIP, for_update=True
        )
        if new_owner_id == user.id:
            raise BadRequestError("You already own this family", code="already_owner")
        target = await self.families.get_active_member(family_id, new_owner_id, for_update=True)
        if target is None:
            raise NotFoundError("Member not found")
        previous_role = target.role
        # Demote first: uq_family_members_active_owner allows one active owner at a time.
        current.role = FamilyRole.ADMIN
        await self.db.flush()
        target.role = FamilyRole.OWNER
        await self.db.flush()
        self._audit(
            "family.ownership_transferred",
            user,
            client,
            family_id=family_id,
            entity_type="family_member",
            entity_id=target.id,
            from_user_id=str(user.id),
            to_user_id=str(new_owner_id),
            to_previous_role=previous_role.value,
        )
        await self.db.commit()
        return await self._detail(await self._live_family(family_id), FamilyRole.ADMIN)

    async def leave_family(self, user: User, family_id: uuid.UUID, client: ClientInfo) -> None:
        member = await require_family_access(self.db, user, family_id, for_update=True)
        if member.role is FamilyRole.OWNER:
            raise ConflictError(
                "Transfer ownership to another member (or delete the family) before leaving",
                code="owner_must_transfer",
            )
        member.left_at = utcnow()
        self._audit(
            "family.member_left",
            user,
            client,
            family_id=family_id,
            entity_type="family_member",
            entity_id=member.id,
            role=member.role.value,
        )
        await self.db.commit()

    # ------------------------------------------------------------------ account deletion hooks

    async def assert_user_can_delete_account(self, user_id: uuid.UUID) -> None:
        """Raise 409 ``family_ownership_transfer_required`` if the user is the owner of a live
        family that still has other active members. Call before deleting an account."""
        blocking = await self.families.families_blocking_account_deletion(user_id)
        if blocking:
            raise ConflictError(
                "Transfer ownership of your families before deleting your account",
                code="family_ownership_transfer_required",
                details={"family_ids": [str(f.id) for f in blocking]},
            )

    async def detach_user_for_account_deletion(self, user_id: uuid.UUID, now: datetime) -> None:
        """End every active membership of a user being deleted; families they solely own are
        soft-deleted (their pending invitations revoked). Does **not** commit — call inside the
        account-deletion transaction, after :meth:`assert_user_can_delete_account`."""
        for membership in await self.families.active_memberships(user_id):
            membership.left_at = now
            if membership.role is FamilyRole.OWNER:
                family = await self.families.get_live(membership.family_id, for_update=True)
                if family is not None:
                    family.deleted_at = now
                    await self.invitations.revoke_all_pending(family.id, now)
            self.audit.record(
                "family.member_left",
                actor_user_id=user_id,
                entity_type="family_member",
                entity_id=membership.id,
                family_id=membership.family_id,
                details={"reason": "account_deleted"},
            )


def raise_forbidden_member_change(target_role: FamilyRole) -> NoReturn:
    if target_role is FamilyRole.OWNER:
        raise ForbiddenError(
            "The owner's role can only change through an ownership transfer",
            code="cannot_manage_owner",
        )
    raise ForbiddenError(
        "Your role does not allow managing this member", code="insufficient_family_role"
    )
