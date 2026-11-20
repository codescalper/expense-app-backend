"""Budgets (spec §8): request/response shapes.

Money is ``Decimal`` in, two-decimal strings out. Percentages are plain numbers (one decimal).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, PlainSerializer, StringConstraints, model_validator

from app.models.enums import BudgetPeriod, RecordScope
from app.schemas.common import RequestModel, ResponseModel

CENT = Decimal("0.01")
MAX_BUDGET_CATEGORIES = 50


def money_str(value: Decimal) -> str:
    """Decimal -> plain string with exactly two decimals (``"1250.50"``)."""
    return f"{value.quantize(CENT):f}"


MoneyOut = Annotated[Decimal, PlainSerializer(money_str, return_type=str)]
PositiveMoneyIn = Annotated[
    Decimal, Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)
]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
BudgetName = Annotated[str, StringConstraints(min_length=1, max_length=80)]


def _thresholds(value: list[int]) -> list[int]:
    if not value:
        raise ValueError("at least one alert threshold is required")
    if any(t < 1 or t > 200 for t in value):
        raise ValueError("alert thresholds must be between 1 and 200")
    return sorted(set(value))


AlertThresholds = Annotated[list[int], Field(max_length=20), AfterValidator(_thresholds)]

BudgetStatus = Literal["ok", "warning", "exceeded"]
PeriodState = Literal["upcoming", "current", "ended"]


class ScopedCreate(RequestModel):
    """``scope=personal`` (default, no ``family_id``) or ``scope=family`` + ``family_id``."""

    scope: RecordScope = RecordScope.PERSONAL
    family_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _scope_family(self) -> Self:
        if self.scope is RecordScope.FAMILY and self.family_id is None:
            raise ValueError("family_id is required when scope is 'family'")
        if self.scope is RecordScope.PERSONAL and self.family_id is not None:
            raise ValueError("family_id must be omitted when scope is 'personal'")
        return self


# --- requests --------------------------------------------------------------------------


class BudgetCategoryIn(RequestModel):
    category_id: uuid.UUID
    amount: PositiveMoneyIn


def _unique_categories(value: list[BudgetCategoryIn]) -> list[BudgetCategoryIn]:
    ids = [c.category_id for c in value]
    if len(ids) != len(set(ids)):
        raise ValueError("each category may appear only once")
    return value


BudgetCategories = Annotated[
    list[BudgetCategoryIn],
    Field(max_length=MAX_BUDGET_CATEGORIES),
    AfterValidator(_unique_categories),
]


class BudgetCreate(ScopedCreate):
    """``start_date`` defaults to today (owner's timezone) for weekly/monthly budgets; custom
    budgets need both ``start_date`` and ``end_date``. ``member_user_id`` is family-only."""

    name: BudgetName
    period: BudgetPeriod = BudgetPeriod.MONTHLY
    start_date: date | None = None
    end_date: date | None = None
    amount: PositiveMoneyIn
    currency: CurrencyCode | None = None
    member_user_id: uuid.UUID | None = None
    carry_forward: bool = False
    alert_thresholds: AlertThresholds = Field(default_factory=lambda: [50, 75, 90, 100])
    is_active: bool = True
    categories: BudgetCategories = Field(default_factory=list)

    @model_validator(mode="after")
    def _rules(self) -> Self:
        if self.period is BudgetPeriod.CUSTOM and (
            self.start_date is None or self.end_date is None
        ):
            raise ValueError("custom budgets need start_date and end_date")
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if self.member_user_id is not None and self.scope is not RecordScope.FAMILY:
            raise ValueError("member_user_id is only allowed on family budgets")
        return self


class BudgetUpdate(RequestModel):
    """Partial update. ``end_date: null`` / ``member_user_id: null`` clear those fields;
    ``categories`` (when present) replaces the whole list. Scope can't change."""

    name: BudgetName | None = None
    period: BudgetPeriod | None = None
    start_date: date | None = None
    end_date: date | None = None
    amount: PositiveMoneyIn | None = None
    member_user_id: uuid.UUID | None = None
    carry_forward: bool | None = None
    alert_thresholds: AlertThresholds | None = None
    is_active: bool | None = None
    categories: BudgetCategories | None = None

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        nullable = {"end_date", "member_user_id"}
        for field in self.model_fields_set - nullable:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


# --- responses -------------------------------------------------------------------------


class BudgetCategoryOut(ResponseModel):
    category_id: uuid.UUID
    name: str
    amount: MoneyOut


class PeriodStatusOut(ResponseModel):
    """Numbers for one budget period. ``limit = amount + carried``; ``percent = spent/limit``
    (100 when the limit is ≤ 0); ``status`` is ``exceeded`` when spent > limit, ``warning`` from
    75 % (inclusive), else ``ok``."""

    period_key: str
    start_date: date
    end_date: date
    state: PeriodState
    limit: MoneyOut
    carried: MoneyOut
    spent: MoneyOut
    remaining: MoneyOut
    percent: float
    status: BudgetStatus
    days_left: int


class CategoryStatusOut(ResponseModel):
    category_id: uuid.UUID
    name: str
    limit: MoneyOut
    spent: MoneyOut
    remaining: MoneyOut
    percent: float
    status: BudgetStatus


class BudgetOut(ResponseModel):
    id: uuid.UUID
    scope: RecordScope
    family_id: uuid.UUID | None
    owner_user_id: uuid.UUID
    member_user_id: uuid.UUID | None
    name: str
    period: BudgetPeriod
    start_date: date
    end_date: date | None
    amount: MoneyOut
    currency: str
    carry_forward: bool
    alert_thresholds: list[int]
    is_active: bool
    categories: list[BudgetCategoryOut]
    can_edit: bool
    created_at: datetime
    updated_at: datetime
    current: PeriodStatusOut


class BudgetDetailOut(BudgetOut):
    category_breakdown: list[CategoryStatusOut]


class BudgetHistoryItem(PeriodStatusOut):
    is_current: bool


class BudgetHistoryOut(ResponseModel):
    budget_id: uuid.UUID
    period: BudgetPeriod
    items: list[BudgetHistoryItem]


class BudgetWarningOut(ResponseModel):
    budget_id: uuid.UUID
    name: str
    period: BudgetPeriod
    member_user_id: uuid.UUID | None
    limit: MoneyOut
    spent: MoneyOut
    remaining: MoneyOut
    percent: float
    status: BudgetStatus


class MonthlySummaryOut(ResponseModel):
    """Dashboard "remaining monthly budget". ``basis``: ``overall`` = active monthly budgets
    without category filters (spent = all spending in the month); ``categories`` = only
    category budgets exist (spent = spending in the union of their categories); ``none``."""

    period_key: str
    start_date: date
    end_date: date
    basis: Literal["overall", "categories", "none"]
    budget_count: int
    total_budget: MoneyOut
    spent: MoneyOut
    remaining: MoneyOut
    percent: float
    status: BudgetStatus


class BudgetOverviewOut(ResponseModel):
    scope: RecordScope
    family_id: uuid.UUID | None
    month: MonthlySummaryOut
    warnings: list[BudgetWarningOut]
