"""UPI intent payments (/api/v1/upi).

Opening a UPI app is never treated as success: only ``/confirm`` (what the user reports)
changes the transaction, and even then only to ``verification=user_confirmed`` — ``verified``
is reserved for a future authorised bank/PSP integration and is never set by this API.

Personal payments are visible to their initiator only; family payments to the family's active
members (404 otherwise). Changes need the initiator or a family owner/admin (403 otherwise).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response, status

from app.api.deps import Client, CurrentPrincipal, DbSession
from app.core.errors import ValidationFailedError
from app.models.enums import UpiAttemptStatus
from app.schemas.upi import (
    OPEN_STATUSES,
    UPI_IDEMPOTENCY_KEY_PATTERN,
    UpiConfirmRequest,
    UpiInitiateRequest,
    UpiParseOut,
    UpiParseRequest,
    UpiPaymentDetailOut,
    UpiPaymentOut,
    UpiReturnRequest,
)
from app.services.upi import UpiService
from app.utils.pagination import Page, Pagination

router = APIRouter(prefix="/upi", tags=["upi"])


def get_upi_service(db: DbSession) -> UpiService:
    return UpiService(db)


UpiSvc = Annotated[UpiService, Depends(get_upi_service)]


@router.post("/parse")
async def parse_qr(body: UpiParseRequest, principal: CurrentPrincipal, svc: UpiSvc) -> UpiParseOut:
    """Validate a scanned ``upi://pay`` payload and enrich it with the known payee and a
    suggested category for the scope. Informational only; nothing is stored."""
    return await svc.parse(principal.user, body)


@router.post("/payments", status_code=status.HTTP_201_CREATED)
async def initiate_payment(
    body: UpiInitiateRequest,
    principal: CurrentPrincipal,
    svc: UpiSvc,
    client: Client,
    response: Response,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            pattern=UPI_IDEMPOTENCY_KEY_PATTERN,
            description="Required (or body idempotency_key). A repeat returns the original (200)",
        ),
    ] = None,
) -> UpiPaymentDetailOut:
    """Create the payment attempt and its **pending** expense, returning the ``upi_uri`` to
    launch. 201 when created, 200 with the original attempt for a repeated key."""
    if idempotency_key is not None:
        if body.idempotency_key is not None and body.idempotency_key != idempotency_key:
            raise ValidationFailedError(
                "Idempotency-Key header and body idempotency_key differ",
                code="idempotency_key_mismatch",
            )
        body = body.model_copy(update={"idempotency_key": idempotency_key})
    out, created = await svc.initiate(principal.user, body, client)
    if not created:
        response.status_code = status.HTTP_200_OK
    return out


@router.get("/payments")
async def list_payments(
    principal: CurrentPrincipal,
    svc: UpiSvc,
    page: Pagination,
    family_id: Annotated[
        uuid.UUID | None, Query(description="Family payments instead of personal ones")
    ] = None,
    status_: Annotated[UpiAttemptStatus | None, Query(alias="status")] = None,
    q: Annotated[
        str | None,
        Query(min_length=1, max_length=100, description="Payee name / UPI ID / note / UTR / ref"),
    ] = None,
    date_from: Annotated[date | None, Query(description="YYYY-MM-DD, user's time zone")] = None,
    date_to: Annotated[date | None, Query(description="YYYY-MM-DD inclusive")] = None,
) -> Page[UpiPaymentOut]:
    """Payment history, newest first."""
    return await svc.list_payments(
        principal.user,
        family_id=family_id,
        statuses=[status_] if status_ else [],
        q=q.strip() if q else None,
        date_from=date_from,
        date_to=date_to,
        page=page,
    )


@router.get("/payments/pending")
async def pending_payments(
    principal: CurrentPrincipal,
    svc: UpiSvc,
    page: Pagination,
    family_id: uuid.UUID | None = None,
) -> Page[UpiPaymentOut]:
    """Payments still waiting for the user's answer (initiated / launched /
    pending_reported), newest first."""
    return await svc.list_payments(
        principal.user,
        family_id=family_id,
        statuses=OPEN_STATUSES,
        q=None,
        date_from=None,
        date_to=None,
        page=page,
    )


@router.get("/payments/{payment_id}")
async def get_payment(
    payment_id: uuid.UUID, principal: CurrentPrincipal, svc: UpiSvc
) -> UpiPaymentDetailOut:
    return await svc.get(principal.user, payment_id)


@router.post("/payments/{payment_id}/launched")
async def payment_launched(
    payment_id: uuid.UUID, principal: CurrentPrincipal, svc: UpiSvc, client: Client
) -> UpiPaymentDetailOut:
    """The UPI app chooser was opened. Does **not** change the transaction."""
    return await svc.mark_launched(principal.user, payment_id, client)


@router.post("/payments/{payment_id}/return")
async def payment_return(
    payment_id: uuid.UUID,
    body: UpiReturnRequest,
    principal: CurrentPrincipal,
    svc: UpiSvc,
    client: Client,
) -> UpiPaymentDetailOut:
    """Store what the UPI app handed back (untrusted, sanitised) and return it as an
    ``untrusted_hint``. Changes no money state — ask the user, then call ``/confirm``."""
    return await svc.record_return(principal.user, payment_id, body, client)


@router.post("/payments/{payment_id}/confirm")
async def confirm_payment(
    payment_id: uuid.UUID,
    body: UpiConfirmRequest,
    principal: CurrentPrincipal,
    svc: UpiSvc,
    client: Client,
) -> UpiPaymentDetailOut:
    """The user's answer: ``success`` completes the transaction (``user_confirmed``, never
    ``verified``), ``failure`` fails it, ``pending`` keeps it pending. Idempotent; a later
    different answer is an audited reconciliation."""
    return await svc.confirm(principal.user, payment_id, body, client)


@router.post("/payments/{payment_id}/cancel")
async def cancel_payment(
    payment_id: uuid.UUID, principal: CurrentPrincipal, svc: UpiSvc, client: Client
) -> UpiPaymentDetailOut:
    """Abandon the payment (not after a reported success)."""
    return await svc.cancel(principal.user, payment_id, client)
