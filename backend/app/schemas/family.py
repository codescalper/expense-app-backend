from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import StringConstraints

from app.models.enums import FamilyRole, InvitationStatus
from app.schemas.common import NormalizedEmail, RequestModel, ResponseModel

FamilyName = Annotated[str, StringConstraints(min_length=1, max_length=80)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
# Roles that can be granted by invitation or role change. "owner" only moves via transfer.
AssignableRole = Literal["admin", "member", "viewer"]
InvitationToken = Annotated[str, StringConstraints(min_length=20, max_length=200)]


# --- requests --------------------------------------------------------------------------


class FamilyCreate(RequestModel):
    name: FamilyName
    currency: CurrencyCode = "INR"


class FamilyUpdate(RequestModel):
    name: FamilyName | None = None
    currency: CurrencyCode | None = None


class InvitationCreate(RequestModel):
    email: NormalizedEmail
    role: AssignableRole = "member"


class InvitationTokenRequest(RequestModel):
    token: InvitationToken


class MemberRoleUpdate(RequestModel):
    role: AssignableRole


class TransferOwnershipRequest(RequestModel):
    user_id: uuid.UUID


# --- responses -------------------------------------------------------------------------


class FamilySummaryOut(ResponseModel):
    id: uuid.UUID
    name: str
    currency: str
    has_image: bool
    my_role: FamilyRole
    member_count: int
    created_at: datetime


class MemberOut(ResponseModel):
    user_id: uuid.UUID
    full_name: str
    email: str
    role: FamilyRole
    has_avatar: bool
    joined_at: datetime


class FamilyDetailOut(ResponseModel):
    id: uuid.UUID
    name: str
    currency: str
    has_image: bool
    my_role: FamilyRole
    created_by_user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    members: list[MemberOut]


class InvitationOut(ResponseModel):
    """An invitation as seen by the family's admins."""

    id: uuid.UUID
    family_id: uuid.UUID
    email: str
    role: FamilyRole
    status: InvitationStatus
    invited_by_user_id: uuid.UUID
    expires_at: datetime
    created_at: datetime


class InvitationCreatedOut(InvitationOut):
    """Returned once, on creation. ``token`` is never retrievable again."""

    token: str
    deep_link: str


class IncomingInvitationOut(ResponseModel):
    """An invitation addressed to the caller's (verified) email address."""

    id: uuid.UUID
    family_id: uuid.UUID
    family_name: str
    role: FamilyRole
    invited_by_name: str
    expires_at: datetime
    created_at: datetime


class InvitationPreviewOut(ResponseModel):
    """What an invitation link points at, shown before the user accepts it."""

    id: uuid.UUID
    family_id: uuid.UUID
    family_name: str
    role: FamilyRole
    invited_by_name: str
    status: InvitationStatus  # "expired" when past expires_at
    expires_at: datetime
    already_member: bool
