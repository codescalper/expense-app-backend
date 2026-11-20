"""Home dashboard (spec §4): one response with every card the Home screen shows."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from app.models.enums import (
    AccountType,
    TransactionSource,
    TransactionStatus,
    TransactionType,
)
from app.schemas.budgets import BudgetOverviewOut, MoneyOut
from app.schemas.common import ResponseModel
from app.schemas.recurring import UpcomingOut
from app.schemas.reports import CategorySlice, IncomeExpensePoint, MemberSpending, ReportMeta
from app.schemas.savings import GoalOut


class AccountBalanceOut(ResponseModel):
    id: uuid.UUID
    name: str
    type: AccountType
    balance: MoneyOut


class BalanceCard(ResponseModel):
    """``available`` = sum of active non-credit-card accounts. Credit cards are listed under
    ``liabilities`` (their balance is usually negative = amount owed)."""

    available: MoneyOut
    liabilities_total: MoneyOut
    net_worth: MoneyOut  # available + liabilities_total
    accounts: list[AccountBalanceOut]
    liabilities: list[AccountBalanceOut]


class PeriodTotals(ResponseModel):
    income: MoneyOut
    expense: MoneyOut
    net_savings: MoneyOut
    savings_rate: float


class SpendingSplit(ResponseModel):
    """Personal view: ``personal`` = my personal spending; ``family`` = spending I recorded in
    my families' shared books (``members`` is empty).
    Family view: ``family`` = the family's total spending, ``personal`` = the part I recorded,
    ``members`` = per-member breakdown."""

    view: Literal["personal", "family"]
    personal: MoneyOut
    family: MoneyOut
    members: list[MemberSpending]


class RecentCategory(ResponseModel):
    id: uuid.UUID
    name: str
    icon: str | None
    color: str | None


class RecentTransaction(ResponseModel):
    id: uuid.UUID
    type: TransactionType
    status: TransactionStatus
    source: TransactionSource
    amount: MoneyOut
    currency: str
    occurred_at: datetime
    description: str | None
    merchant: str | None
    payee_name: str | None
    account_name: str | None
    category: RecentCategory | None
    created_by: uuid.UUID
    created_by_name: str
    # Set on rows created by the UPI flow: open /upi/payments/{id} (e.g. to confirm a pending
    # payment) straight from the Home screen.
    upi_attempt_id: uuid.UUID | None


class SavingsCard(ResponseModel):
    active_count: int
    target_total: MoneyOut
    saved_total: MoneyOut
    goals: list[GoalOut]  # top 3 active goals by progress


class DashboardOut(ResponseModel):
    meta: ReportMeta
    balance: BalanceCard
    totals: PeriodTotals
    spending_split: SpendingSplit
    budget: BudgetOverviewOut
    recent_transactions: list[RecentTransaction]
    upcoming_bills: list[UpcomingOut]
    savings: SavingsCard
    spending_by_category: list[CategorySlice]
    monthly_trend: list[IncomeExpensePoint]
    pending_upi_count: int
