"""Transactions (spec §5): create/update payloads, list filters and response shapes."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    Field,
    StringConstraints,
    model_validator,
)

from app.models.enums import (
    AccountType,
    CategoryKind,
    PaymentMethod,
    RecordScope,
    TransactionSource,
    TransactionStatus,
    TransactionType,
    VerificationStatus,
)
from app.schemas.accounts import CurrencyCode, MoneyOut, ScopedCreate
from app.schemas.common import RequestModel, ResponseModel

MAX_TAGS = 10
MAX_TAG_LENGTH = 30
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9._:\-]{1,100}$"

# Strictly positive, at most 2 decimals, fits NUMERIC(14,2).
AmountIn = Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)]
IdempotencyKey = Annotated[str, StringConstraints(pattern=IDEMPOTENCY_KEY_PATTERN)]
Merchant = Annotated[str, StringConstraints(max_length=120)]
Description = Annotated[str, StringConstraints(max_length=255)]
Notes = Annotated[str, StringConstraints(max_length=2000)]
AdjustmentSign = Literal[-1, 1]


def normalize_tags(tags: list[str]) -> list[str]:
    """Strip, lowercase and de-duplicate (order kept); ≤10 tags of 1-30 characters."""
    out: list[str] = []
    for raw in tags:
        tag = raw.strip().lower()
        if not tag:
            raise ValueError("tags cannot be empty")
        if len(tag) > MAX_TAG_LENGTH:
            raise ValueError(f"tags can be at most {MAX_TAG_LENGTH} characters")
        if tag not in out:
            out.append(tag)
    if len(out) > MAX_TAGS:
        raise ValueError(f"at most {MAX_TAGS} tags are allowed")
    return out


Tags = Annotated[list[str], AfterValidator(normalize_tags)]


# --- requests --------------------------------------------------------------------------


class TransactionCreate(ScopedCreate):
    """New transaction.

    * ``currency`` defaults to the account's currency (``INR`` when no account).
    * ``occurred_at`` defaults to now; a value without a UTC offset is read in the caller's
      time zone.
    * ``status`` may be ``completed`` (default) or ``pending``.
    * ``idempotency_key`` may also be sent as the ``Idempotency-Key`` header.
    """

    type: TransactionType
    amount: AmountIn
    currency: CurrencyCode | None = None
    account_id: uuid.UUID | None = None
    to_account_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    payee_id: uuid.UUID | None = None
    merchant: Merchant | None = None
    description: Description | None = None
    notes: Notes | None = None
    payment_method: PaymentMethod | None = None
    occurred_at: datetime | None = None
    adjustment_sign: AdjustmentSign | None = None
    tags: Tags = Field(default_factory=list)
    status: Literal[TransactionStatus.COMPLETED, TransactionStatus.PENDING] = (
        TransactionStatus.COMPLETED
    )
    idempotency_key: IdempotencyKey | None = None


class TransactionUpdate(RequestModel):
    """Partial update; send only the fields that change. ``null`` clears optional fields.

    ``scope`` / ``family_id`` may be echoed back but must not change (a transaction can't move
    between personal and family). ``status`` can only be changed on manually created rows.
    """

    scope: RecordScope | None = None
    family_id: uuid.UUID | None = None
    type: TransactionType | None = None
    amount: AmountIn | None = None
    currency: CurrencyCode | None = None
    account_id: uuid.UUID | None = None
    to_account_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    payee_id: uuid.UUID | None = None
    merchant: Merchant | None = None
    description: Description | None = None
    notes: Notes | None = None
    payment_method: PaymentMethod | None = None
    occurred_at: datetime | None = None
    adjustment_sign: AdjustmentSign | None = None
    tags: Tags | None = None
    status: TransactionStatus | None = None

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        for name in ("type", "amount", "currency", "occurred_at", "tags", "status"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


# --- list filters ----------------------------------------------------------------------

SortField = Literal["occurred_at", "amount"]
SortDir = Literal["asc", "desc"]


@dataclass(frozen=True)
class TransactionFilters:
    """Query-string filters shared by list, summary and CSV export."""

    family_id: uuid.UUID | None = None
    date_from: date | None = None
    date_to: date | None = None
    types: list[TransactionType] = field(default_factory=list)
    category_ids: list[uuid.UUID] = field(default_factory=list)
    account_id: uuid.UUID | None = None
    payment_method: PaymentMethod | None = None
    member_user_id: uuid.UUID | None = None
    status: TransactionStatus | None = None
    q: str | None = None
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None
    tag: str | None = None


# --- responses -------------------------------------------------------------------------


class UserRef(ResponseModel):
    id: uuid.UUID
    full_name: str


class AccountRef(ResponseModel):
    id: uuid.UUID
    name: str
    type: AccountType
    is_active: bool


class CategoryRef(ResponseModel):
    id: uuid.UUID
    name: str
    kind: CategoryKind
    icon: str | None
    color: str | None
    parent_id: uuid.UUID | None


class PayeeRef(ResponseModel):
    id: uuid.UUID
    name: str
    upi_id: str | None


class TransactionOut(ResponseModel):
    id: uuid.UUID
    type: TransactionType
    status: TransactionStatus
    source: TransactionSource
    verification: VerificationStatus
    amount: MoneyOut
    currency: str
    scope: RecordScope
    family_id: uuid.UUID | None
    created_by: UserRef
    account: AccountRef | None
    to_account: AccountRef | None
    category: CategoryRef | None
    payee: PayeeRef | None
    merchant: str | None
    description: str | None
    notes: str | None
    payment_method: PaymentMethod | None
    occurred_at: datetime
    adjustment_sign: int | None
    tags: list[str]
    attachments_count: int
    recurring_id: uuid.UUID | None
    period_key: str | None
    upi_attempt_id: uuid.UUID | None
    external_ref: str | None
    idempotency_key: str | None
    can_edit: bool
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class TransactionSummaryOut(ResponseModel):
    """Totals over the filtered rows. ``income``/``expense``/``net`` follow the ledger rules
    (completed, non-deleted rows only; transfers & adjustments excluded; refunds reduce
    expense). ``count`` is the number of matching rows of any status."""

    income: MoneyOut
    expense: MoneyOut
    net: MoneyOut
    count: int
