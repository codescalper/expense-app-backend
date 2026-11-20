"""Payee endpoints (/api/v1/payees).

``family_id`` absent → my personal payees; present → that family's payees (membership required,
else 404).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import Client, CurrentPrincipal, DbSession
from app.schemas.payees import CategorySuggestionOut, PayeeCreate, PayeeOut, PayeeUpdate
from app.services.payees import PayeeService
from app.utils.pagination import Page, Pagination

router = APIRouter(prefix="/payees", tags=["payees"])


def get_payee_service(db: DbSession) -> PayeeService:
    return PayeeService(db)


PayeeSvc = Annotated[PayeeService, Depends(get_payee_service)]
ShortLimit = Annotated[int, Query(ge=1, le=50)]


@router.get("")
async def list_payees(
    principal: CurrentPrincipal,
    svc: PayeeSvc,
    page: Pagination,
    family_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=100, description="Name / UPI ID / mobile")] = None,
) -> Page[PayeeOut]:
    return await svc.list_payees(principal.user, family_id, q, page)


@router.get("/recent")
async def recent_payees(
    principal: CurrentPrincipal,
    svc: PayeeSvc,
    family_id: uuid.UUID | None = None,
    limit: ShortLimit = 10,
) -> list[PayeeOut]:
    """Most recently paid first (payees never used are left out)."""
    return await svc.recent(principal.user, family_id, limit)


@router.get("/frequent")
async def frequent_payees(
    principal: CurrentPrincipal,
    svc: PayeeSvc,
    family_id: uuid.UUID | None = None,
    limit: ShortLimit = 10,
) -> list[PayeeOut]:
    """Most used first (payees never used are left out)."""
    return await svc.frequent(principal.user, family_id, limit)


@router.get("/suggest-category")
async def suggest_category(
    principal: CurrentPrincipal,
    svc: PayeeSvc,
    family_id: uuid.UUID | None = None,
    upi_id: Annotated[str | None, Query(max_length=320)] = None,
    payee_id: uuid.UUID | None = None,
    merchant: Annotated[str | None, Query(max_length=120)] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> CategorySuggestionOut:
    """Best category for a payment: history → payee default → keywords → none."""
    return await svc.suggest_category(
        principal.user, family_id, upi_id=upi_id, payee_id=payee_id, merchant=merchant, q=q
    )


@router.get("/{payee_id}")
async def get_payee(payee_id: uuid.UUID, principal: CurrentPrincipal, svc: PayeeSvc) -> PayeeOut:
    return await svc.get(principal.user, payee_id)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_payee(
    body: PayeeCreate, principal: CurrentPrincipal, svc: PayeeSvc, client: Client
) -> PayeeOut:
    return await svc.create(principal.user, body, client)


@router.patch("/{payee_id}")
async def update_payee(
    payee_id: uuid.UUID,
    body: PayeeUpdate,
    principal: CurrentPrincipal,
    svc: PayeeSvc,
    client: Client,
) -> PayeeOut:
    return await svc.update(principal.user, payee_id, body, client)


@router.delete("/{payee_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_payee(
    payee_id: uuid.UUID, principal: CurrentPrincipal, svc: PayeeSvc, client: Client
) -> None:
    await svc.delete(principal.user, payee_id, client)
