"""Family management endpoints (/api/v1/families).

Non-members get 404 for every ``/families/{family_id}/...`` route; members whose role is too
low get 403 (see ``app.core.permissions``).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile, status
from fastapi.responses import Response

from app.api.deps import AppSettings, Client, CurrentPrincipal, DbSession
from app.api.files import stored_file_response
from app.core.errors import PayloadTooLargeError
from app.models.enums import FamilyRole
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
    InvitationTokenRequest,
    MemberRoleUpdate,
    TransferOwnershipRequest,
)
from app.services.families import FamilyService

router = APIRouter(prefix="/families", tags=["families"])


def get_family_service(db: DbSession, settings: AppSettings) -> FamilyService:
    return FamilyService(db, settings)


FamilySvc = Annotated[FamilyService, Depends(get_family_service)]


# --- invitations addressed to me (declared before /{family_id} routes) -------------------


@router.get("/invitations/incoming")
async def list_incoming_invitations(
    principal: CurrentPrincipal, svc: FamilySvc
) -> list[IncomingInvitationOut]:
    """Pending, unexpired invitations sent to my verified email address."""
    return await svc.list_incoming(principal.user)


@router.post("/invitations/preview")
async def preview_invitation(
    body: InvitationTokenRequest, principal: CurrentPrincipal, svc: FamilySvc
) -> InvitationPreviewOut:
    """Look up an invitation link token before accepting it."""
    return await svc.preview_by_token(principal.user, body.token)


@router.post("/invitations/accept")
async def accept_invitation_by_token(
    body: InvitationTokenRequest, principal: CurrentPrincipal, svc: FamilySvc, client: Client
) -> FamilySummaryOut:
    """Join via an invitation link token (single use; any signed-in user holding it)."""
    return await svc.accept_by_token(principal.user, body.token, client)


@router.post("/invitations/reject", status_code=status.HTTP_204_NO_CONTENT)
async def reject_invitation_by_token(
    body: InvitationTokenRequest, principal: CurrentPrincipal, svc: FamilySvc, client: Client
) -> None:
    await svc.reject_by_token(principal.user, body.token, client)


@router.post("/invitations/{invitation_id}/accept")
async def accept_invitation(
    invitation_id: uuid.UUID, principal: CurrentPrincipal, svc: FamilySvc, client: Client
) -> FamilySummaryOut:
    """Accept an invitation addressed to my verified email (404 for anyone else's)."""
    return await svc.accept_invitation(principal.user, invitation_id, client)


@router.post("/invitations/{invitation_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
async def reject_invitation(
    invitation_id: uuid.UUID, principal: CurrentPrincipal, svc: FamilySvc, client: Client
) -> None:
    await svc.reject_invitation(principal.user, invitation_id, client)


# --- families ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_family(
    body: FamilyCreate, principal: CurrentPrincipal, svc: FamilySvc, client: Client
) -> FamilyDetailOut:
    """Create a family; the caller becomes its owner."""
    return await svc.create_family(principal.user, body, client)


@router.get("")
async def list_families(principal: CurrentPrincipal, svc: FamilySvc) -> list[FamilySummaryOut]:
    return await svc.list_families(principal.user)


@router.get("/{family_id}")
async def get_family(
    family_id: uuid.UUID, principal: CurrentPrincipal, svc: FamilySvc
) -> FamilyDetailOut:
    return await svc.get_family(principal.user, family_id)


@router.patch("/{family_id}")
async def update_family(
    family_id: uuid.UUID,
    body: FamilyUpdate,
    principal: CurrentPrincipal,
    svc: FamilySvc,
    client: Client,
) -> FamilyDetailOut:
    """Owner/admin: rename or change the default currency."""
    return await svc.update_family(principal.user, family_id, body, client)


@router.delete("/{family_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_family(
    family_id: uuid.UUID, principal: CurrentPrincipal, svc: FamilySvc, client: Client
) -> None:
    """Owner only. Soft-deletes the family for everyone."""
    await svc.delete_family(principal.user, family_id, client)


# --- family image -----------------------------------------------------------------------


@router.put("/{family_id}/image")
async def upload_family_image(
    family_id: uuid.UUID,
    principal: CurrentPrincipal,
    svc: FamilySvc,
    client: Client,
    settings: AppSettings,
    file: UploadFile = File(..., description="JPEG, PNG or WebP, max 2 MB"),
) -> FamilyDetailOut:
    raw = await file.read(settings.max_avatar_bytes + 1)
    if len(raw) > settings.max_avatar_bytes:
        raise PayloadTooLargeError(
            f"Image must be at most {settings.max_avatar_bytes / 1024 / 1024:g} MB"
        )
    return await svc.set_image(principal.user, family_id, raw, client)


@router.get("/{family_id}/image", response_class=Response)
async def get_family_image(
    family_id: uuid.UUID, principal: CurrentPrincipal, svc: FamilySvc
) -> Response:
    return stored_file_response(
        await svc.image_file(principal.user, family_id),
        media_type="image/jpeg",
        disposition="inline",
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.delete("/{family_id}/image", status_code=status.HTTP_204_NO_CONTENT)
async def delete_family_image(
    family_id: uuid.UUID, principal: CurrentPrincipal, svc: FamilySvc, client: Client
) -> None:
    await svc.remove_image(principal.user, family_id, client)


# --- invitations (admin side) -----------------------------------------------------------


@router.post("/{family_id}/invitations", status_code=status.HTTP_201_CREATED)
async def create_invitation(
    family_id: uuid.UUID,
    body: InvitationCreate,
    principal: CurrentPrincipal,
    svc: FamilySvc,
    client: Client,
) -> InvitationCreatedOut:
    """Owner/admin. Emails the invitee; the link ``token`` is returned only in this response.
    Re-inviting the same email replaces (revokes) the previous pending invitation."""
    return await svc.create_invitation(principal.user, family_id, body, client)


@router.get("/{family_id}/invitations")
async def list_invitations(
    family_id: uuid.UUID, principal: CurrentPrincipal, svc: FamilySvc
) -> list[InvitationOut]:
    """Owner/admin: pending, unexpired invitations."""
    return await svc.list_invitations(principal.user, family_id)


@router.delete("/{family_id}/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invitation(
    family_id: uuid.UUID,
    invitation_id: uuid.UUID,
    principal: CurrentPrincipal,
    svc: FamilySvc,
    client: Client,
) -> None:
    await svc.revoke_invitation(principal.user, family_id, invitation_id, client)


# --- members ----------------------------------------------------------------------------


@router.patch("/{family_id}/members/{user_id}")
async def change_member_role(
    family_id: uuid.UUID,
    user_id: uuid.UUID,
    body: MemberRoleUpdate,
    principal: CurrentPrincipal,
    svc: FamilySvc,
    client: Client,
) -> FamilyDetailOut:
    """Owner/admin. The owner's role can't be changed here, and nobody can grant ``owner``."""
    return await svc.change_member_role(
        principal.user, family_id, user_id, FamilyRole(body.role), client
    )


@router.delete("/{family_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    family_id: uuid.UUID,
    user_id: uuid.UUID,
    principal: CurrentPrincipal,
    svc: FamilySvc,
    client: Client,
) -> None:
    """Owner/admin. The owner can't be removed; use /leave to remove yourself."""
    await svc.remove_member(principal.user, family_id, user_id, client)


@router.post("/{family_id}/transfer-ownership")
async def transfer_ownership(
    family_id: uuid.UUID,
    body: TransferOwnershipRequest,
    principal: CurrentPrincipal,
    svc: FamilySvc,
    client: Client,
) -> FamilyDetailOut:
    """Owner only. The target becomes owner; the caller becomes admin."""
    return await svc.transfer_ownership(principal.user, family_id, body.user_id, client)


@router.post("/{family_id}/leave", status_code=status.HTTP_204_NO_CONTENT)
async def leave_family(
    family_id: uuid.UUID, principal: CurrentPrincipal, svc: FamilySvc, client: Client
) -> None:
    """Any member except the owner (409 ``owner_must_transfer``)."""
    await svc.leave_family(principal.user, family_id, client)
