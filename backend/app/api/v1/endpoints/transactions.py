"""Transactions (/api/v1/transactions).

List/summary/export take the same filters. Without ``family_id`` they cover the caller's
personal rows; with it, that family's rows (404 if not a member). Rows outside the caller's
visibility are 404; family rows the caller may see but not edit are 403.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response, status
from fastapi.responses import StreamingResponse

from app.api.deps import Client, CurrentPrincipal, DbSession, user_rate_limit
from app.core.errors import ValidationFailedError
from app.core.security import utcnow
from app.models.enums import PaymentMethod, TransactionStatus, TransactionType
from app.schemas.transactions import (
    IDEMPOTENCY_KEY_PATTERN,
    MAX_TAG_LENGTH,
    SortDir,
    SortField,
    TransactionCreate,
    TransactionFilters,
    TransactionOut,
    TransactionSummaryOut,
    TransactionUpdate,
)
from app.services.transactions import TransactionService
from app.utils.pagination import Page, Pagination

router = APIRouter(prefix="/transactions", tags=["transactions"])


def get_transaction_service(db: DbSession) -> TransactionService:
    return TransactionService(db)


TransactionSvc = Annotated[TransactionService, Depends(get_transaction_service)]


def transaction_filters(
    family_id: Annotated[
        uuid.UUID | None, Query(description="Family rows instead of personal ones")
    ] = None,
    date_from: Annotated[
        date | None, Query(description="YYYY-MM-DD, inclusive, in the user's time zone")
    ] = None,
    date_to: Annotated[
        date | None, Query(description="YYYY-MM-DD, inclusive, in the user's time zone")
    ] = None,
    types: Annotated[
        list[TransactionType] | None, Query(description="Repeat: ?types=expense&types=refund")
    ] = None,
    category_ids: Annotated[
        list[uuid.UUID] | None, Query(description="Repeatable; includes subcategories")
    ] = None,
    account_id: Annotated[
        uuid.UUID | None, Query(description="Either side of a transfer matches")
    ] = None,
    payment_method: PaymentMethod | None = None,
    member_user_id: Annotated[
        uuid.UUID | None, Query(description="Created by this family member")
    ] = None,
    status: TransactionStatus | None = None,
    q: Annotated[
        str | None,
        Query(min_length=1, max_length=100, description="Search description/merchant/notes/payee"),
    ] = None,
    min_amount: Annotated[Decimal | None, Query(ge=0, max_digits=14, decimal_places=2)] = None,
    max_amount: Annotated[Decimal | None, Query(ge=0, max_digits=14, decimal_places=2)] = None,
    tag: Annotated[str | None, Query(min_length=1, max_length=MAX_TAG_LENGTH)] = None,
) -> TransactionFilters:
    return TransactionFilters(
        family_id=family_id,
        date_from=date_from,
        date_to=date_to,
        types=types or [],
        category_ids=category_ids or [],
        account_id=account_id,
        payment_method=payment_method,
        member_user_id=member_user_id,
        status=status,
        q=q.strip() if q else None,
        min_amount=min_amount,
        max_amount=max_amount,
        tag=tag.strip().lower() if tag else None,
    )


Filters = Annotated[TransactionFilters, Depends(transaction_filters)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_transaction(
    body: TransactionCreate,
    principal: CurrentPrincipal,
    svc: TransactionSvc,
    client: Client,
    response: Response,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            pattern=IDEMPOTENCY_KEY_PATTERN,
            description="Retry-safe key; a repeat returns the original transaction (200)",
        ),
    ] = None,
) -> TransactionOut:
    """201 with the new transaction, or 200 with the original one for a repeated
    idempotency key."""
    if idempotency_key is not None:
        if body.idempotency_key is not None and body.idempotency_key != idempotency_key:
            raise ValidationFailedError(
                "Idempotency-Key header and body idempotency_key differ",
                code="idempotency_key_mismatch",
            )
        body = body.model_copy(update={"idempotency_key": idempotency_key})
    out, created = await svc.create(principal.user, body, client)
    if not created:
        response.status_code = status.HTTP_200_OK
    return out


@router.get("")
async def list_transactions(
    principal: CurrentPrincipal,
    svc: TransactionSvc,
    filters: Filters,
    page: Pagination,
    sort_by: SortField = "occurred_at",
    sort_dir: SortDir = "desc",
) -> Page[TransactionOut]:
    return await svc.list_transactions(
        principal.user, filters, sort_by=sort_by, sort_dir=sort_dir, page=page
    )


@router.get("/summary")
async def transactions_summary(
    principal: CurrentPrincipal, svc: TransactionSvc, filters: Filters
) -> TransactionSummaryOut:
    """Income / expense / net (ledger rules) and row count for the same filters as the list."""
    return await svc.summary(principal.user, filters)


@router.get(
    "/export.csv",
    response_class=StreamingResponse,
    dependencies=[Depends(user_rate_limit("export"))],
)
async def export_transactions(
    principal: CurrentPrincipal, svc: TransactionSvc, filters: Filters
) -> StreamingResponse:
    """CSV of the filtered rows (newest first), dates in the user's time zone."""
    lines = await svc.export_csv(principal.user, filters)
    filename = f"transactions-{utcnow():%Y%m%d}.csv"
    return StreamingResponse(
        lines,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{transaction_id}")
async def get_transaction(
    transaction_id: uuid.UUID, principal: CurrentPrincipal, svc: TransactionSvc
) -> TransactionOut:
    return await svc.get(principal.user, transaction_id)


@router.patch("/{transaction_id}")
async def update_transaction(
    transaction_id: uuid.UUID,
    body: TransactionUpdate,
    principal: CurrentPrincipal,
    svc: TransactionSvc,
    client: Client,
) -> TransactionOut:
    return await svc.update(principal.user, transaction_id, body, client)


@router.delete("/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_transaction(
    transaction_id: uuid.UUID, principal: CurrentPrincipal, svc: TransactionSvc, client: Client
) -> None:
    """Soft delete; undo with ``POST /transactions/{id}/restore``."""
    await svc.delete(principal.user, transaction_id, client)


@router.post("/{transaction_id}/restore")
async def restore_transaction(
    transaction_id: uuid.UUID, principal: CurrentPrincipal, svc: TransactionSvc, client: Client
) -> TransactionOut:
    return await svc.restore(principal.user, transaction_id, client)
