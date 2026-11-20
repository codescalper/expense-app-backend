"""Accounts & payment methods (/api/v1/accounts).

Accounts outside the caller's visibility are 404; family accounts need owner/admin
(``MANAGE_SHARED_SETUP``) to create/edit/archive/delete (403 otherwise).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import Client, CurrentPrincipal, DbSession
from app.schemas.accounts import AccountCreate, AccountOut, AccountUpdate
from app.services.accounts import AccountService

router = APIRouter(prefix="/accounts", tags=["accounts"])


def get_account_service(db: DbSession) -> AccountService:
    return AccountService(db)


AccountSvc = Annotated[AccountService, Depends(get_account_service)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_account(
    body: AccountCreate, principal: CurrentPrincipal, svc: AccountSvc, client: Client
) -> AccountOut:
    """Create a personal account, or a family account (owner/admin only)."""
    return await svc.create_account(principal.user, body, client)


@router.get("")
async def list_accounts(
    principal: CurrentPrincipal,
    svc: AccountSvc,
    family_id: Annotated[
        uuid.UUID | None, Query(description="Family accounts instead of personal ones")
    ] = None,
    include_inactive: Annotated[bool, Query(description="Include archived accounts")] = False,
) -> list[AccountOut]:
    """Accounts with their computed ``current_balance``, active first, then by name."""
    return await svc.list_accounts(principal.user, family_id, include_inactive=include_inactive)


@router.get("/{account_id}")
async def get_account(
    account_id: uuid.UUID, principal: CurrentPrincipal, svc: AccountSvc
) -> AccountOut:
    return await svc.get_account(principal.user, account_id)


@router.patch("/{account_id}")
async def update_account(
    account_id: uuid.UUID,
    body: AccountUpdate,
    principal: CurrentPrincipal,
    svc: AccountSvc,
    client: Client,
) -> AccountOut:
    return await svc.update_account(principal.user, account_id, body, client)


@router.post("/{account_id}/archive")
async def archive_account(
    account_id: uuid.UUID, principal: CurrentPrincipal, svc: AccountSvc, client: Client
) -> AccountOut:
    return await svc.set_active(principal.user, account_id, False, client)


@router.post("/{account_id}/unarchive")
async def unarchive_account(
    account_id: uuid.UUID, principal: CurrentPrincipal, svc: AccountSvc, client: Client
) -> AccountOut:
    return await svc.set_active(principal.user, account_id, True, client)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: uuid.UUID, principal: CurrentPrincipal, svc: AccountSvc, client: Client
) -> None:
    """Soft delete. 409 ``account_in_use`` while live transactions reference it."""
    await svc.delete_account(principal.user, account_id, client)
