"""Savings goals and contributions (spec §10)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Literal, Self

from pydantic import StringConstraints, model_validator

from app.models.enums import ContributionKind, GoalStatus, RecordScope
from app.schemas.budgets import CurrencyCode, MoneyOut, PositiveMoneyIn, ScopedCreate
from app.schemas.common import RequestModel, ResponseModel

GoalName = Annotated[str, StringConstraints(min_length=1, max_length=80)]
Icon = Annotated[str, StringConstraints(min_length=1, max_length=40)]
Color = Annotated[str, StringConstraints(pattern=r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")]
Note = Annotated[str, StringConstraints(max_length=500)]

MILESTONES = (25, 50, 75, 100)


# --- requests --------------------------------------------------------------------------


class GoalCreate(ScopedCreate):
    name: GoalName
    target_amount: PositiveMoneyIn
    target_date: date | None = None
    currency: CurrencyCode | None = None
    icon: Icon | None = None
    color: Color | None = None


class GoalUpdate(RequestModel):
    """Partial update. ``target_date`` / ``icon`` / ``color`` may be set to null to clear.
    ``status`` accepts ``archived`` or ``active`` (``completed`` is automatic: an active goal
    whose balance reaches the target becomes completed, and goes back to active if the balance
    drops below it)."""

    name: GoalName | None = None
    target_amount: PositiveMoneyIn | None = None
    target_date: date | None = None
    icon: Icon | None = None
    color: Color | None = None
    status: Literal["active", "archived"] | None = None

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        for field in ("name", "target_amount", "status"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class ContributionCreate(RequestModel):
    kind: ContributionKind = ContributionKind.DEPOSIT
    amount: PositiveMoneyIn
    note: Note | None = None
    contributed_at: datetime | None = None
    transaction_id: uuid.UUID | None = None


# --- responses -------------------------------------------------------------------------


class PacingOut(ResponseModel):
    """How much to save per month to hit ``target_date``. ``months_left`` counts whole months
    from today (owner's timezone) to the target date, minimum 1; when the date has passed,
    ``overdue`` is true and the whole remaining amount is due now."""

    target_date: date
    days_left: int
    months_left: int
    required_monthly: MoneyOut
    overdue: bool


class GoalOut(ResponseModel):
    id: uuid.UUID
    scope: RecordScope
    family_id: uuid.UUID | None
    owner_user_id: uuid.UUID
    name: str
    target_amount: MoneyOut
    current_amount: MoneyOut
    remaining_amount: MoneyOut
    progress_percent: float
    target_date: date | None
    currency: str
    status: GoalStatus
    icon: str | None
    color: str | None
    completed_at: datetime | None
    pacing: PacingOut | None
    can_edit: bool
    can_contribute: bool
    created_at: datetime
    updated_at: datetime


class ContributionOut(ResponseModel):
    """``user_name``: who added it. ``can_delete``: whether the caller may delete it (personal
    goal: its owner; family goal: owner/admin, or the member who added it)."""

    id: uuid.UUID
    goal_id: uuid.UUID
    user_id: uuid.UUID
    user_name: str
    kind: ContributionKind
    amount: MoneyOut
    note: str | None
    contributed_at: datetime
    transaction_id: uuid.UUID | None
    can_delete: bool
    created_at: datetime


class ContributionCreatedOut(ResponseModel):
    contribution: ContributionOut
    goal: GoalOut


class GoalHistoryPoint(ResponseModel):
    month: str  # "2026-11"
    deposits: MoneyOut
    withdrawals: MoneyOut
    net: MoneyOut
    balance: MoneyOut  # cumulative at month end


class GoalHistoryOut(ResponseModel):
    goal_id: uuid.UUID
    items: list[GoalHistoryPoint]
