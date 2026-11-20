"""Request/response models for recurring rules and their occurrences (/api/v1/recurring)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from app.models.enums import (
    PaymentMethod,
    RecordScope,
    RecurrenceFrequency,
    TransactionStatus,
    TransactionType,
    VerificationStatus,
)
from app.schemas.common import RequestModel, ResponseModel

RuleName = Annotated[str, StringConstraints(min_length=1, max_length=80)]
Description = Annotated[str, StringConstraints(max_length=255)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
ExternalRef = Annotated[str, StringConstraints(min_length=1, max_length=64)]
# NUMERIC(14,2): strictly positive, at most 2 decimal places. JSON accepts "1499.00" or 1499.
PositiveMoney = Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2)]
IntervalCount = Annotated[int, Field(ge=1, le=365)]
ReminderDays = Annotated[int, Field(ge=0, le=60)]
RuleType = Literal["expense", "income", "transfer"]
RuleStatus = Literal["active", "paused", "ended"]
# "scheduled" = not generated yet; otherwise the generated transaction's status.
OccurrenceState = Literal["scheduled", "pending", "completed", "cancelled", "failed"]


def _check_accounts(
    type_: str | None, account_id: uuid.UUID | None, to_account_id: uuid.UUID | None
) -> None:
    if type_ == TransactionType.TRANSFER:
        if account_id is None or to_account_id is None:
            raise ValueError("transfers need both account_id and to_account_id")
        if account_id == to_account_id:
            raise ValueError("to_account_id must differ from account_id")
    elif to_account_id is not None:
        raise ValueError("to_account_id is only allowed for transfers")


# --- requests --------------------------------------------------------------------------


class RecurringCreate(RequestModel):
    scope: RecordScope = RecordScope.PERSONAL
    family_id: uuid.UUID | None = None
    name: RuleName
    type: RuleType
    amount: PositiveMoney
    currency: CurrencyCode | None = None  # default: the family's currency, else INR
    account_id: uuid.UUID | None = None
    to_account_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    payee_id: uuid.UUID | None = None
    description: Description | None = None
    payment_method: PaymentMethod | None = None
    frequency: RecurrenceFrequency
    interval_count: IntervalCount = 1
    start_date: date
    end_date: date | None = None
    reminder_days_before: ReminderDays = 1
    auto_generate: bool = True

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.scope is RecordScope.FAMILY and self.family_id is None:
            raise ValueError("family_id is required when scope is 'family'")
        if self.scope is RecordScope.PERSONAL and self.family_id is not None:
            raise ValueError("family_id must be omitted when scope is 'personal'")
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        _check_accounts(self.type, self.account_id, self.to_account_id)
        if self.type == "transfer" and self.category_id is not None:
            raise ValueError("transfers have no category")
        return self


class RecurringUpdate(RequestModel):
    """Partial update. Send ``null`` to clear an optional field (e.g. ``end_date``).
    Scope / family cannot change. The merged rule is validated like a create."""

    name: RuleName | None = None
    type: RuleType | None = None
    amount: PositiveMoney | None = None
    currency: CurrencyCode | None = None
    account_id: uuid.UUID | None = None
    to_account_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    payee_id: uuid.UUID | None = None
    description: Description | None = None
    payment_method: PaymentMethod | None = None
    frequency: RecurrenceFrequency | None = None
    interval_count: IntervalCount | None = None
    start_date: date | None = None
    end_date: date | None = None
    reminder_days_before: ReminderDays | None = None
    auto_generate: bool | None = None

    @model_validator(mode="after")
    def _no_null_required(self) -> Self:
        required = (
            "name",
            "type",
            "amount",
            "currency",
            "frequency",
            "interval_count",
            "start_date",
            "reminder_days_before",
            "auto_generate",
        )
        for field in required:
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class MarkPaidRequest(RequestModel):
    """All optional: the actual amount / time / account if they differ from the bill."""

    amount: PositiveMoney | None = None
    occurred_at: datetime | None = None
    account_id: uuid.UUID | None = None
    external_ref: ExternalRef | None = None

    @model_validator(mode="after")
    def _aware(self) -> Self:
        if self.occurred_at is not None and self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must include a timezone offset")
        return self


# --- responses -------------------------------------------------------------------------


class LatestOccurrenceOut(ResponseModel):
    transaction_id: uuid.UUID
    period_key: str
    due_date: date
    status: TransactionStatus
    amount: Decimal


class RecurringOut(ResponseModel):
    id: uuid.UUID
    scope: RecordScope
    family_id: uuid.UUID | None
    owner_user_id: uuid.UUID
    name: str
    type: TransactionType
    amount: Decimal
    currency: str
    account_id: uuid.UUID | None
    to_account_id: uuid.UUID | None
    category_id: uuid.UUID | None
    payee_id: uuid.UUID | None
    description: str | None
    payment_method: PaymentMethod | None
    frequency: RecurrenceFrequency
    interval_count: int
    start_date: date
    end_date: date | None
    next_due_date: date | None  # null once the schedule has ended
    reminder_days_before: int
    auto_generate: bool
    is_paused: bool
    status: RuleStatus
    last_generated_at: datetime | None
    latest_occurrence: LatestOccurrenceOut | None
    # Whether the caller may edit / pause / delete the rule and mark its occurrences
    # (personal: owner; family: owner/admin, or the member who created it).
    can_edit: bool
    created_at: datetime
    updated_at: datetime


class OccurrenceOut(ResponseModel):
    """A transaction generated by a recurring rule."""

    transaction_id: uuid.UUID
    recurring_id: uuid.UUID
    period_key: str
    due_date: date
    status: TransactionStatus
    verification: VerificationStatus
    type: TransactionType
    amount: Decimal
    currency: str
    account_id: uuid.UUID | None
    to_account_id: uuid.UUID | None
    category_id: uuid.UUID | None
    payee_id: uuid.UUID | None
    occurred_at: datetime
    external_ref: str | None
    created_at: datetime
    updated_at: datetime


class UpcomingOut(ResponseModel):
    """One scheduled occurrence (generated or not) for the dashboard's upcoming bills."""

    recurring_id: uuid.UUID
    name: str
    scope: RecordScope
    family_id: uuid.UUID | None
    type: TransactionType
    amount: Decimal  # the generated transaction's amount when there is one
    currency: str
    account_id: uuid.UUID | None
    category_id: uuid.UUID | None
    payee_id: uuid.UUID | None
    due_date: date
    period_key: str
    days_until_due: int  # negative = overdue
    is_overdue: bool
    status: OccurrenceState
    transaction_id: uuid.UUID | None
