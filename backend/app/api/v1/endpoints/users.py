from __future__ import annotations

import uuid

from fastapi import APIRouter, File, UploadFile, status
from fastapi.responses import FileResponse

from app.api.deps import AppSettings, Client, CurrentPrincipal, UserSvc
from app.core.errors import PayloadTooLargeError
from app.schemas.user import (
    ChangePasswordRequest,
    DeleteAccountRequest,
    SessionOut,
    UserOut,
    UserUpdate,
)

router = APIRouter(prefix="/users/me", tags=["users"])


@router.get("")
async def get_me(principal: CurrentPrincipal) -> UserOut:
    return UserOut.from_user(principal.user)


@router.patch("")
async def update_me(
    body: UserUpdate, principal: CurrentPrincipal, svc: UserSvc, client: Client
) -> UserOut:
    user = await svc.update_profile(principal.user, body, client)
    return UserOut.from_user(user)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordRequest, principal: CurrentPrincipal, svc: UserSvc, client: Client
) -> None:
    """Keeps this device signed in and signs out all others.

    Wrong current password: 400 ``invalid_password`` (not 401). Throttled per user (429)."""
    await svc.change_password(principal.user, principal.session_id, body, client)


@router.put("/avatar")
async def upload_avatar(
    principal: CurrentPrincipal,
    svc: UserSvc,
    client: Client,
    settings: AppSettings,
    file: UploadFile = File(..., description="JPEG, PNG or WebP, max 2 MB"),
) -> UserOut:
    # Read at most limit+1 bytes so an oversized upload can't exhaust memory.
    raw = await file.read(settings.max_avatar_bytes + 1)
    if len(raw) > settings.max_avatar_bytes:
        raise PayloadTooLargeError(
            f"Image must be at most {settings.max_avatar_bytes / 1024 / 1024:g} MB"
        )
    user = await svc.set_avatar(principal.user, raw, client)
    return UserOut.from_user(user)


@router.get("/avatar", response_class=FileResponse)
async def get_avatar(principal: CurrentPrincipal, svc: UserSvc) -> FileResponse:
    return FileResponse(
        svc.avatar_path(principal.user),
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.delete("/avatar", status_code=status.HTTP_204_NO_CONTENT)
async def delete_avatar(principal: CurrentPrincipal, svc: UserSvc, client: Client) -> None:
    await svc.remove_avatar(principal.user, client)


@router.get("/sessions")
async def list_sessions(principal: CurrentPrincipal, svc: UserSvc) -> list[SessionOut]:
    return await svc.list_sessions(principal.user, principal.session_id)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: uuid.UUID, principal: CurrentPrincipal, svc: UserSvc, client: Client
) -> None:
    await svc.revoke_session(principal.user, session_id, client)


@router.post("/delete", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    body: DeleteAccountRequest, principal: CurrentPrincipal, svc: UserSvc, client: Client
) -> None:
    """Permanently deletes (anonymises) the account. Requires the current password.
    POST rather than DELETE because a request body is required.

    Wrong password: 400 ``invalid_password``; throttled per user (429). The owner of a family
    that still has other members gets 409 ``family_ownership_transfer_required``."""
    await svc.delete_account(principal.user, body.password, client)
