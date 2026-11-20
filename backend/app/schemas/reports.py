"""Reports (spec §12): response shapes shared by ``/reports/*`` and the dashboard.

Money is two-decimal strings (``"1250.50"``); percentages are numbers with one decimal. Every
report carries a :class:`ReportMeta` block saying which scope, currency, timezone and date range
the figures cover.
"""

from __future__ import annotations

import datetime as dt
import uuid
from datetime import date
from typing import Literal

from app.models.enums import (
    AccountType,
    BudgetPeriod,
    RecordScope,
    RecurrenceFrequency,
    TransactionType,
    UpiAttemptStatus,
)
from app.schemas.budgets import BudgetHistoryItem, MoneyOut
from app.schemas.common import ResponseModel

Preset = Literal["today", "this_week", "this_month", "last_month", "custom"]
Granularity = Literal["day", "week", "month", "year"]
CategoryReportKind = Literal["expense", "income"]
REPORT_NAMES = (
    "spending",
    "income-vs-expense",
    "categories",
    "members",
    "personal-vs-family",
    "payment-methods",
    "account-balances",
    "savings-trend",
    "budget-performance",
    "recurring-summary",
    "upi",
)
ReportName = Literal[
    "spending",
    "income-vs-expense",
    "categories",
    "members",
    "personal-vs-family",
    "payment-methods",
    "account-balances",
    "savings-trend",
    "budget-performance",
    "recurring-summary",
    "upi",
]


class RangeOut(ResponseModel):
    """Local (caller's timezone) dates, both inclusive."""

    preset: Preset
    start_date: date
    end_date: date


class ReportMeta(ResponseModel):
    scope: RecordScope
    family_id: uuid.UUID | None
    currency: str
    # Every currency that has transactions or accounts in this scope (most used first). Figures
    # only ever cover ``currency``; pass ``?currency=`` to see another one.
    currencies: list[str]
    timezone: str
    range: RangeOut


# --- spending / income vs expense --------------------------------------------------------


class SpendingPoint(ResponseModel):
    period: str  # "2026-11-20" | "2026-W47" | "2026-11" | "2026"
    start_date: date
    end_date: date
    amount: MoneyOut


class SpendingReport(ResponseModel):
    meta: ReportMeta
    granularity: Granularity
    total: MoneyOut
    average: MoneyOut  # total / number of buckets
    items: list[SpendingPoint]


class IncomeExpensePoint(ResponseModel):
    month: str  # "2026-11"
    start_date: date
    end_date: date
    income: MoneyOut
    expense: MoneyOut
    net: MoneyOut


class IncomeExpenseReport(ResponseModel):
    meta: ReportMeta
    income: MoneyOut
    expense: MoneyOut
    net: MoneyOut
    savings_rate: float  # net / income * 100 (0 when there is no income)
    items: list[IncomeExpensePoint]


# --- categories ----------------------------------------------------------------------------


class CategorySlice(ResponseModel):
    category_id: uuid.UUID | None  # null = uncategorised (or the "Other" bucket on the dashboard)
    name: str
    icon: str | None
    color: str | None
    amount: MoneyOut
    count: int
    percent: float
    has_children: bool = False  # drill-down available via ?parent_id=
    is_other: bool = False


class CategoryReport(ResponseModel):
    meta: ReportMeta
    kind: CategoryReportKind
    parent: CategorySlice | None  # set when drilling down (?parent_id=)
    total: MoneyOut
    items: list[CategorySlice]


# --- members / personal vs family ---------------------------------------------------------


class MemberSpending(ResponseModel):
    user_id: uuid.UUID
    full_name: str
    is_active_member: bool
    expense: MoneyOut
    income: MoneyOut
    count: int
    percent: float  # share of the family's spending


class MembersReport(ResponseModel):
    meta: ReportMeta
    total: MoneyOut
    items: list[MemberSpending]


class PersonalFamilyPoint(ResponseModel):
    month: str
    start_date: date
    end_date: date
    personal: MoneyOut
    family: MoneyOut


class FamilyShare(ResponseModel):
    family_id: uuid.UUID
    name: str
    amount: MoneyOut


class PersonalVsFamilyReport(ResponseModel):
    meta: ReportMeta
    personal: MoneyOut  # my personal spending
    family: MoneyOut  # spending I recorded in my families' shared books
    families: list[FamilyShare]
    items: list[PersonalFamilyPoint]


# --- payment methods / account balances -------------------------------------------------------


class PaymentMethodSlice(ResponseModel):
    payment_method: str  # cash|upi|card|bank_transfer|wallet|other|unspecified
    amount: MoneyOut
    count: int
    percent: float


class PaymentMethodsReport(ResponseModel):
    meta: ReportMeta
    total: MoneyOut
    items: list[PaymentMethodSlice]


class BalancePoint(ResponseModel):
    date: dt.date
    balance: MoneyOut  # end-of-day balance
    change: MoneyOut  # net movement on that day


class AccountBalanceReport(ResponseModel):
    meta: ReportMeta
    account_id: uuid.UUID
    name: str
    type: AccountType
    is_active: bool
    opening_balance: MoneyOut
    current_balance: MoneyOut
    items: list[BalancePoint]


# --- savings / budgets / recurring ----------------------------------------------------------


class SavingsPoint(ResponseModel):
    month: str
    start_date: date
    end_date: date
    deposits: MoneyOut
    withdrawals: MoneyOut
    net: MoneyOut
    balance: MoneyOut  # cumulative across all goals at month end


class SavingsTrendReport(ResponseModel):
    meta: ReportMeta
    goal_count: int
    target_total: MoneyOut
    current_total: MoneyOut
    items: list[SavingsPoint]


class BudgetPerformance(ResponseModel):
    budget_id: uuid.UUID
    name: str
    period: BudgetPeriod
    currency: str
    member_user_id: uuid.UUID | None
    average_percent: float
    exceeded_count: int
    items: list[BudgetHistoryItem]


class BudgetPerformanceReport(ResponseModel):
    meta: ReportMeta
    budgets: list[BudgetPerformance]


class RecurringRuleSummary(ResponseModel):
    recurring_id: uuid.UUID
    name: str
    type: TransactionType
    frequency: RecurrenceFrequency
    interval_count: int
    amount: MoneyOut
    monthly_amount: MoneyOut
    category_id: uuid.UUID | None
    next_due_date: date


class RecurringCategory(ResponseModel):
    category_id: uuid.UUID | None
    name: str
    icon: str | None
    color: str | None
    monthly_amount: MoneyOut
    rule_count: int
    percent: float


class RecurringFrequency(ResponseModel):
    frequency: RecurrenceFrequency
    monthly_amount: MoneyOut
    rule_count: int


class RecurringSummaryReport(ResponseModel):
    meta: ReportMeta
    active_rules: int
    monthly_expense: MoneyOut
    monthly_income: MoneyOut
    monthly_transfer: MoneyOut
    by_category: list[RecurringCategory]  # expense rules
    by_frequency: list[RecurringFrequency]  # expense rules
    rules: list[RecurringRuleSummary]


# --- UPI ----------------------------------------------------------------------------------------


class UpiStatusCount(ResponseModel):
    status: UpiAttemptStatus
    count: int
    amount: MoneyOut


class UpiPayee(ResponseModel):
    payee_vpa: str
    payee_name: str | None
    count: int
    amount: MoneyOut


class UpiReport(ResponseModel):
    meta: ReportMeta
    attempts: int
    attempted_amount: MoneyOut
    success_reported: int
    success_reported_amount: MoneyOut
    success_reported_rate: float  # success_reported / attempts * 100
    open_attempts: int  # initiated | launched | pending_reported
    pending_confirmation: int  # transactions still pending (source upi_intent), any date
    confirmed_spend: MoneyOut  # counted expense of transactions linked to attempts
    by_status: list[UpiStatusCount]
    top_payees: list[UpiPayee]
    by_category: list[CategorySlice]
