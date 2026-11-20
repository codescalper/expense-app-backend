"""Reports (spec §12) and their CSV export.

Conventions shared by every report (and the dashboard):

* **Scope.** ``family_id`` absent → the caller's *personal* rows; present → that family's rows
  (active membership required, otherwise 404 — indistinguishable from a missing family).
* **Dates.** ``preset`` = ``today | this_week | this_month | last_month | custom`` (weeks are
  Monday..Sunday) or ``from``/``to`` (``YYYY-MM-DD``, both inclusive, ``custom`` implied). Dates
  are local dates in the **caller's** ``users.timezone``; they are converted to a half-open UTC
  range on ``occurred_at`` and rows are bucketed by ``occurred_at AT TIME ZONE tz``. Month-trend
  reports (income-vs-expense, personal-vs-family, savings-trend) default to the last ``months``
  calendar months when no explicit range is given; everything else defaults to ``this_month``.
* **Accounting.** All money comes from :mod:`app.services.ledger`: only live ``completed`` rows
  count; expense = expenses - refunds (a refund reduces its category's spending); transfers and
  adjustments are never income or expense; pending/failed/cancelled/deleted rows never count.
* **Currency.** Figures are never summed across currencies. Each response covers one
  ``currency`` (``?currency=``; default: the family's currency, or for personal scope the most
  used currency, ``INR`` when there is no data) and lists every currency with data in
  ``meta.currencies`` so a client can switch.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, and_, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationFailedError
from app.core.permissions import (
    FamilyPermission,
    ScopeFilter,
    require_family_access,
    visible_categories_clause,
)
from app.core.security import utcnow
from app.models.enums import (
    AccountType,
    RecordScope,
    RecurrenceFrequency,
    TransactionType,
    UpiAttemptStatus,
)
from app.models.finance import Transaction, TransactionCategory
from app.models.user import User
from app.repositories.budgets import BudgetRepository
from app.repositories.recurring import RecurringRepository
from app.repositories.reports import OPEN_UPI_STATUSES, CategoryRow, ReportRepository, money
from app.repositories.savings import SavingsRepository
from app.schemas.reports import (
    AccountBalanceReport,
    BalancePoint,
    BudgetPerformance,
    BudgetPerformanceReport,
    CategoryReport,
    CategoryReportKind,
    CategorySlice,
    FamilyShare,
    Granularity,
    IncomeExpensePoint,
    IncomeExpenseReport,
    MemberSpending,
    MembersReport,
    PaymentMethodSlice,
    PaymentMethodsReport,
    PersonalFamilyPoint,
    PersonalVsFamilyReport,
    Preset,
    RangeOut,
    RecurringCategory,
    RecurringFrequency,
    RecurringRuleSummary,
    RecurringSummaryReport,
    ReportMeta,
    ReportName,
    SavingsPoint,
    SavingsTrendReport,
    SpendingPoint,
    SpendingReport,
    UpiPayee,
    UpiReport,
    UpiStatusCount,
)
from app.services.budgets import BudgetService, zone
from app.services.ledger import account_balances, expense_amount, income_amount, is_spending
from app.services.recurring import rule_status
from app.utils.csv_safety import neutralize_formula

ZERO = Decimal("0.00")
DEFAULT_CURRENCY = "INR"
MAX_RANGE_DAYS = 3660  # ~10 years
MAX_BUCKETS = 1000
MAX_MONTHS = 60
MAX_BALANCE_DAYS = 730
MAX_BUDGET_PERIODS = 24
UNCATEGORIZED = "Uncategorized"
OTHER = "Other"

# Average number of occurrences per month for each frequency (before ``interval_count``).
MONTHLY_FACTOR: dict[RecurrenceFrequency, Decimal] = {
    RecurrenceFrequency.DAILY: Decimal(365) / Decimal(12),
    RecurrenceFrequency.WEEKLY: Decimal(52) / Decimal(12),
    RecurrenceFrequency.MONTHLY: Decimal(1),
    RecurrenceFrequency.YEARLY: Decimal(1) / Decimal(12),
}


# --- pure helpers ------------------------------------------------------------------------


def pct(part: Decimal, whole: Decimal) -> float:
    """``part / whole`` as a percentage with one decimal (0 when ``whole`` ≤ 0)."""
    if whole <= 0:
        return 0.0
    return float((part * 100 / whole).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def month_start(day: date) -> date:
    return day.replace(day=1)


def add_months(day: date, months: int) -> date:
    """First day of the month ``months`` after ``day``'s month."""
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def month_end(day: date) -> date:
    return add_months(day, 1) - timedelta(days=1)


def bucket_start(day: date, unit: Granularity) -> date:
    if unit == "day":
        return day
    if unit == "week":
        return day - timedelta(days=day.weekday())
    if unit == "month":
        return day.replace(day=1)
    return date(day.year, 1, 1)


def bucket_end(start: date, unit: Granularity) -> date:
    if unit == "day":
        return start
    if unit == "week":
        return start + timedelta(days=6)
    if unit == "month":
        return month_end(start)
    return date(start.year, 12, 31)


def next_bucket(start: date, unit: Granularity) -> date:
    return bucket_end(start, unit) + timedelta(days=1)


def bucket_label(start: date, unit: Granularity) -> str:
    if unit == "day":
        return start.isoformat()
    if unit == "week":
        iso = start.isocalendar()
        return f"{iso.year:04d}-W{iso.week:02d}"
    if unit == "month":
        return f"{start.year:04d}-{start.month:02d}"
    return f"{start.year:04d}"


def bucket_count(first: date, last: date, unit: Granularity) -> int:
    if unit == "day":
        return (last - first).days + 1
    if unit == "week":
        return (last - first).days // 7 + 1
    if unit == "month":
        return (last.year - first.year) * 12 + last.month - first.month + 1
    return last.year - first.year + 1


def utc_range(start: date, end: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """``[local midnight of start, local midnight after end)`` in UTC."""
    lo = datetime.combine(start, time.min, tzinfo=tz).astimezone(UTC)
    hi = datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)
    return lo, hi


def resolve_range(
    preset: Preset | None,
    date_from: date | None,
    date_to: date | None,
    today: date,
    *,
    default_months: int | None = None,
) -> tuple[Preset, date, date]:
    """``(preset, start, end)`` for the request (see the module docstring)."""
    has_dates = date_from is not None or date_to is not None
    if preset is not None and preset != "custom" and has_dates:
        raise ValidationFailedError(
            "Use either a preset or from/to dates, not both", code="range_conflict"
        )
    if preset == "custom" or has_dates:
        if date_from is None or date_to is None:
            raise ValidationFailedError(
                "A custom range needs both from and to", code="range_required"
            )
        if date_from > date_to:
            raise ValidationFailedError("from must not be after to", code="range_invalid")
        if (date_to - date_from).days + 1 > MAX_RANGE_DAYS:
            raise ValidationFailedError(
                f"The range may cover at most {MAX_RANGE_DAYS} days", code="range_too_large"
            )
        return "custom", date_from, date_to
    if preset is None and default_months is not None:
        first = add_months(month_start(today), -(default_months - 1))
        return "custom", first, month_end(today)
    preset = preset or "this_month"
    if preset == "today":
        return preset, today, today
    if preset == "this_week":
        start = today - timedelta(days=today.weekday())
        return preset, start, start + timedelta(days=6)
    if preset == "this_month":
        return preset, month_start(today), month_end(today)
    last = month_start(today) - timedelta(days=1)
    return "last_month", month_start(last), last


def csv_cell(value: Any) -> Any:
    """Text cells go through :func:`app.utils.csv_safety.neutralize_formula` (spreadsheet
    formula injection). Numbers/dates we generate are written as-is."""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return f"{value.quantize(Decimal('0.01')):f}"
    if isinstance(value, bool | int | float | date | uuid.UUID):
        return str(value)
    return neutralize_formula(str(value))


def iter_csv(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> Iterator[str]:
    """CSV text, one line per chunk (for ``StreamingResponse``)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    for row in [list(header), *rows]:
        writer.writerow([csv_cell(v) for v in row])
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)


def slice_of(row: CategoryRow, total: Decimal) -> CategorySlice:
    return CategorySlice(
        category_id=row.category_id,
        name=row.name or UNCATEGORIZED,
        icon=row.icon,
        color=row.color,
        amount=row.amount,
        count=row.count,
        percent=pct(row.amount, total),
        has_children=row.has_children,
    )


def top_with_other(items: list[CategorySlice], total: Decimal, n: int) -> list[CategorySlice]:
    """The ``n`` largest slices plus one ``Other`` slice summing the rest."""
    if len(items) <= n + 1:
        return items
    head, tail = items[:n], items[n:]
    rest = sum((i.amount for i in tail), ZERO)
    other = CategorySlice(
        category_id=None,
        name=OTHER,
        icon=None,
        color=None,
        amount=rest,
        count=sum(i.count for i in tail),
        percent=pct(rest, total),
        is_other=True,
    )
    return [*head, other]


# --- context -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportParams:
    """Every query parameter a report may take (the CSV export passes them all through)."""

    family_id: uuid.UUID | None = None
    preset: Preset | None = None
    date_from: date | None = None
    date_to: date | None = None
    currency: str | None = None
    granularity: Granularity = "month"
    months: int = 12
    kind: CategoryReportKind = "expense"
    parent_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    days: int = 90
    periods: int = 6


@dataclass(frozen=True)
class ReportContext:
    user: User
    family_id: uuid.UUID | None
    scope: ScopeFilter
    tz: ZoneInfo
    currency: str
    currencies: list[str]
    preset: Preset
    start: date
    end: date
    today: date

    @property
    def tz_name(self) -> str:
        return self.tz.key

    @property
    def utc(self) -> tuple[datetime, datetime]:
        return utc_range(self.start, self.end, self.tz)

    def tx_where(self, *, dated: bool = True) -> list[ColumnElement[bool]]:
        """Visibility + currency (+ the date range) for ``transactions``."""
        t = Transaction
        out: list[ColumnElement[bool]] = [self.scope.clause(t), t.currency == self.currency]
        if dated:
            lo, hi = self.utc
            out += [t.occurred_at >= lo, t.occurred_at < hi]
        return out

    def mine_in_families(self) -> ColumnElement[bool]:
        """Family rows (of families I'm an active member of) that I recorded."""
        t = Transaction
        family = ScopeFilter.for_user(self.user.id, scope=RecordScope.FAMILY).clause(t)
        return and_(family, t.owner_user_id == self.user.id)

    def pending_upi_where(self) -> list[ColumnElement[bool]]:
        """Rows the caller is asked to confirm: personal view → my personal rows and family
        rows I recorded; family view → the family's rows."""
        if self.family_id is not None:
            return [self.scope.clause(Transaction)]
        return [or_(self.scope.clause(Transaction), self.mine_in_families())]

    def meta(
        self, *, start: date | None = None, end: date | None = None, preset: Preset | None = None
    ) -> ReportMeta:
        return ReportMeta(
            scope=RecordScope.FAMILY if self.family_id else RecordScope.PERSONAL,
            family_id=self.family_id,
            currency=self.currency,
            currencies=self.currencies,
            timezone=self.tz_name,
            range=RangeOut(
                preset=preset or self.preset,
                start_date=start or self.start,
                end_date=end or self.end,
            ),
        )


def _currency(value: str | None) -> str | None:
    if value is None:
        return None
    code = value.strip().upper()
    if len(code) != 3 or not code.isalpha() or not code.isascii():
        raise ValidationFailedError("currency must be a 3-letter code", code="invalid_currency")
    return code


# --- service -----------------------------------------------------------------------------


class ReportService:
    def __init__(self, session: AsyncSession) -> None:
        self.db = session
        self.repo = ReportRepository(session)

    async def context(
        self,
        user: User,
        params: ReportParams,
        *,
        default_months: int | None = None,
        now: datetime | None = None,
    ) -> ReportContext:
        tz = zone(user.timezone)
        today = (now or utcnow()).astimezone(tz).date()
        preset, start, end = resolve_range(
            params.preset, params.date_from, params.date_to, today, default_months=default_months
        )
        currency = _currency(params.currency)
        if params.family_id is not None:
            await require_family_access(self.db, user, params.family_id, FamilyPermission.VIEW)
            scope = ScopeFilter.for_user(user.id, family_id=params.family_id)
        else:
            scope = ScopeFilter.for_user(user.id, scope=RecordScope.PERSONAL)
        currencies = await self.repo.currencies(scope)
        if currency is None:
            if params.family_id is not None:
                currency = await self.repo.family_currency(params.family_id)
            currency = currency or (currencies[0] if currencies else DEFAULT_CURRENCY)
        return ReportContext(
            user=user,
            family_id=params.family_id,
            scope=scope,
            tz=tz,
            currency=currency,
            currencies=currencies,
            preset=preset,
            start=start,
            end=end,
            today=today,
        )

    # ------------------------------------------------------------------ series

    async def spending_series(
        self, ctx: ReportContext, unit: Granularity
    ) -> list[tuple[date, Decimal]]:
        first, last = bucket_start(ctx.start, unit), bucket_start(ctx.end, unit)
        if bucket_count(first, last, unit) > MAX_BUCKETS:
            raise ValidationFailedError(
                f"Too many {unit} buckets for this range (max {MAX_BUCKETS}); "
                "use a coarser granularity",
                code="too_many_buckets",
            )
        lo, hi = ctx.utc
        rows = await self.repo.bucketed(
            {"amount": expense_amount()},
            [*ctx.tx_where(dated=False), is_spending()],
            tz_name=ctx.tz_name,
            unit=unit,
            first_bucket=first,
            last_bucket=last,
            start_utc=lo,
            end_utc=hi,
        )
        return [(b, v["amount"]) for b, v in rows]

    async def monthly_income_expense(self, ctx: ReportContext) -> list[IncomeExpensePoint]:
        lo, hi = ctx.utc
        rows = await self.repo.bucketed(
            {"income": income_amount(), "expense": expense_amount()},
            ctx.tx_where(dated=False),
            tz_name=ctx.tz_name,
            unit="month",
            first_bucket=month_start(ctx.start),
            last_bucket=month_start(ctx.end),
            start_utc=lo,
            end_utc=hi,
        )
        return [
            IncomeExpensePoint(
                month=bucket_label(b, "month"),
                start_date=max(b, ctx.start),
                end_date=min(month_end(b), ctx.end),
                income=v["income"],
                expense=v["expense"],
                net=v["income"] - v["expense"],
            )
            for b, v in rows
        ]

    # ------------------------------------------------------------------ reports

    async def spending(self, ctx: ReportContext, unit: Granularity) -> SpendingReport:
        series = await self.spending_series(ctx, unit)
        total = sum((a for _, a in series), ZERO)
        items = [
            SpendingPoint(
                period=bucket_label(b, unit),
                start_date=max(b, ctx.start),
                end_date=min(bucket_end(b, unit), ctx.end),
                amount=a,
            )
            for b, a in series
        ]
        average = (total / len(items)).quantize(Decimal("0.01"), ROUND_HALF_UP) if items else ZERO
        return SpendingReport(
            meta=ctx.meta(), granularity=unit, total=total, average=average, items=items
        )

    async def income_vs_expense(self, ctx: ReportContext) -> IncomeExpenseReport:
        items = await self.monthly_income_expense(ctx)
        income = sum((i.income for i in items), ZERO)
        expense = sum((i.expense for i in items), ZERO)
        return IncomeExpenseReport(
            meta=ctx.meta(),
            income=income,
            expense=expense,
            net=income - expense,
            savings_rate=pct(income - expense, income),
            items=items,
        )

    async def category_slices(
        self,
        ctx: ReportContext,
        kind: CategoryReportKind = "expense",
        parent_id: uuid.UUID | None = None,
    ) -> tuple[list[CategorySlice], Decimal]:
        rows = await self.repo.category_totals(ctx.tx_where(), kind=kind, parent_id=parent_id)
        rows = [r for r in rows if r.amount != 0]
        total = sum((r.amount for r in rows), ZERO)
        return [slice_of(r, total) for r in rows], total

    async def categories(
        self,
        ctx: ReportContext,
        kind: CategoryReportKind = "expense",
        parent_id: uuid.UUID | None = None,
    ) -> CategoryReport:
        category: TransactionCategory | None = None
        if parent_id is not None:
            category = await self.db.scalar(
                select(TransactionCategory).where(
                    TransactionCategory.id == parent_id,
                    visible_categories_clause(ctx.user.id),
                )
            )
            if category is None:
                raise NotFoundError("Category not found")
        items, total = await self.category_slices(ctx, kind, parent_id)
        parent = (
            CategorySlice(
                category_id=category.id,
                name=category.name,
                icon=category.icon,
                color=category.color,
                amount=total,
                count=sum(i.count for i in items),
                percent=100.0 if total > 0 else 0.0,
                has_children=True,
            )
            if category is not None
            else None
        )
        return CategoryReport(meta=ctx.meta(), kind=kind, parent=parent, total=total, items=items)

    async def member_spending(self, ctx: ReportContext) -> tuple[list[MemberSpending], Decimal]:
        if ctx.family_id is None:
            raise ValidationFailedError(
                "This report needs a family_id", code="family_scope_required"
            )
        rows = {r.user_id: r for r in await self.repo.member_totals(ctx.tx_where())}
        active = await self.repo.active_members(ctx.family_id)
        total = sum((r.expense for r in rows.values()), ZERO)
        items: list[MemberSpending] = []
        active_ids = set()
        for uid, name in active:
            active_ids.add(uid)
            r = rows.get(uid)
            items.append(
                MemberSpending(
                    user_id=uid,
                    full_name=name,
                    is_active_member=True,
                    expense=r.expense if r else ZERO,
                    income=r.income if r else ZERO,
                    count=r.count if r else 0,
                    percent=pct(r.expense, total) if r else 0.0,
                )
            )
        for uid, r in rows.items():
            if uid not in active_ids:  # former members keep their recorded history
                items.append(
                    MemberSpending(
                        user_id=uid,
                        full_name=r.full_name,
                        is_active_member=False,
                        expense=r.expense,
                        income=r.income,
                        count=r.count,
                        percent=pct(r.expense, total),
                    )
                )
        items.sort(key=lambda m: (-m.expense, m.full_name.lower()))
        return items, total

    async def members(self, ctx: ReportContext) -> MembersReport:
        items, total = await self.member_spending(ctx)
        return MembersReport(meta=ctx.meta(), total=total, items=items)

    async def personal_vs_family(self, ctx: ReportContext) -> PersonalVsFamilyReport:
        if ctx.family_id is not None:
            raise ValidationFailedError(
                "This report is only available in the personal view", code="personal_scope_only"
            )
        t = Transaction
        lo, hi = ctx.utc
        personal = ctx.scope.clause(t)
        family = ctx.mine_in_families()
        rows = await self.repo.bucketed(
            {
                "personal": case((t.scope == RecordScope.PERSONAL, expense_amount()), else_=0),
                "family": case((t.scope == RecordScope.FAMILY, expense_amount()), else_=0),
            },
            [or_(personal, family), t.currency == ctx.currency, is_spending()],
            tz_name=ctx.tz_name,
            unit="month",
            first_bucket=month_start(ctx.start),
            last_bucket=month_start(ctx.end),
            start_utc=lo,
            end_utc=hi,
        )
        families = await self.repo.family_spending(
            [family, t.currency == ctx.currency, t.occurred_at >= lo, t.occurred_at < hi]
        )
        items = [
            PersonalFamilyPoint(
                month=bucket_label(b, "month"),
                start_date=max(b, ctx.start),
                end_date=min(month_end(b), ctx.end),
                personal=v["personal"],
                family=v["family"],
            )
            for b, v in rows
        ]
        return PersonalVsFamilyReport(
            meta=ctx.meta(),
            personal=sum((i.personal for i in items), ZERO),
            family=sum((i.family for i in items), ZERO),
            families=[FamilyShare(family_id=f, name=n, amount=a) for f, n, a in families],
            items=items,
        )

    async def payment_methods(self, ctx: ReportContext) -> PaymentMethodsReport:
        rows = [r for r in await self.repo.payment_methods(ctx.tx_where()) if r[1] != 0]
        total = sum((r[1] for r in rows), ZERO)
        return PaymentMethodsReport(
            meta=ctx.meta(),
            total=total,
            items=[
                PaymentMethodSlice(payment_method=m, amount=a, count=n, percent=pct(a, total))
                for m, a, n in rows
            ],
        )

    async def account_balances(
        self, user: User, account_id: uuid.UUID | None, days: int, now: datetime | None = None
    ) -> AccountBalanceReport:
        """End-of-day balances for the last ``days`` local days (today last). Computed
        backwards from the ledger's current balance: ``balance(d) = current - Σ changes after
        d``. Completed rows dated in the future are part of the current balance, so the last
        point equals ``current_balance`` unless such rows exist."""
        if account_id is None:
            raise ValidationFailedError("account_id is required", code="account_required")
        if not 1 <= days <= MAX_BALANCE_DAYS:
            raise ValidationFailedError(
                f"days must be between 1 and {MAX_BALANCE_DAYS}", code="invalid_days"
            )
        account = await self.repo.visible_account(user.id, account_id)
        if account is None:
            raise NotFoundError("Account not found")
        tz = zone(user.timezone)
        today = (now or utcnow()).astimezone(tz).date()
        start = today - timedelta(days=days - 1)
        current = (await account_balances(self.db, [account.id]))[account.id]
        changes = await self.repo.account_daily_changes(
            account.id, tz.key, utc_range(start, start, tz)[0]
        )
        balance = current - sum((v for d, v in changes.items() if d > today), ZERO)
        points: list[BalancePoint] = []
        day = today
        while day >= start:
            change = changes.get(day, ZERO)
            points.append(BalancePoint(date=day, balance=balance, change=change))
            balance -= change
            day -= timedelta(days=1)
        points.reverse()
        meta = ReportMeta(
            scope=account.scope,
            family_id=account.family_id,
            currency=account.currency,
            currencies=[account.currency],
            timezone=tz.key,
            range=RangeOut(preset="custom", start_date=start, end_date=today),
        )
        return AccountBalanceReport(
            meta=meta,
            account_id=account.id,
            name=account.name,
            type=AccountType(account.type),
            is_active=account.is_active,
            opening_balance=account.opening_balance,
            current_balance=current,
            items=points,
        )

    async def savings_trend(self, ctx: ReportContext) -> SavingsTrendReport:
        goals = await self.repo.scope_goals(ctx.scope, ctx.currency)
        ids = [g.id for g in goals]
        lo, hi = ctx.utc
        balance, months = await self.repo.savings_by_month(ids, ctx.tz_name, lo, hi)
        balances = await SavingsRepository(self.db).balances(ids)
        items: list[SavingsPoint] = []
        month = month_start(ctx.start)
        while month <= ctx.end:
            dep, wd = months.get(month, (ZERO, ZERO))
            balance += dep - wd
            items.append(
                SavingsPoint(
                    month=bucket_label(month, "month"),
                    start_date=max(month, ctx.start),
                    end_date=min(month_end(month), ctx.end),
                    deposits=dep,
                    withdrawals=wd,
                    net=dep - wd,
                    balance=balance,
                )
            )
            month = add_months(month, 1)
        return SavingsTrendReport(
            meta=ctx.meta(),
            goal_count=len(goals),
            target_total=sum((g.target_amount for g in goals), ZERO),
            current_total=sum(balances.values(), ZERO),
            items=items,
        )

    async def budget_performance(
        self, ctx: ReportContext, periods: int, now: datetime | None = None
    ) -> BudgetPerformanceReport:
        if not 1 <= periods <= MAX_BUDGET_PERIODS:
            raise ValidationFailedError(
                f"periods must be between 1 and {MAX_BUDGET_PERIODS}", code="invalid_periods"
            )
        budgets = await BudgetRepository(self.db).list_for_scope(
            ctx.user.id, ctx.family_id, is_active=True
        )
        svc = BudgetService(self.db)
        out: list[BudgetPerformance] = []
        for b in budgets:
            history = await svc.history(ctx.user, b.id, periods, now=now)
            items = history.items
            avg = Decimal(str(sum(i.percent for i in items))) / len(items) if items else Decimal(0)
            out.append(
                BudgetPerformance(
                    budget_id=b.id,
                    name=b.name,
                    period=b.period,
                    currency=b.currency,
                    member_user_id=b.member_user_id,
                    average_percent=float(avg.quantize(Decimal("0.1"), ROUND_HALF_UP)),
                    exceeded_count=sum(1 for i in items if i.status == "exceeded"),
                    items=items,
                )
            )
        starts = [i.start_date for b in out for i in b.items]
        ends = [i.end_date for b in out for i in b.items]
        meta = ctx.meta(start=min(starts), end=max(ends), preset="custom") if starts else ctx.meta()
        return BudgetPerformanceReport(meta=meta, budgets=out)

    async def recurring_summary(self, ctx: ReportContext) -> RecurringSummaryReport:
        rules = [
            r
            for r in await RecurringRepository(self.db).rules_for_scope(ctx.scope)
            if rule_status(r) == "active" and r.currency == ctx.currency
        ]
        cat_ids = [r.category_id for r in rules if r.category_id is not None]
        cats = await self.repo.categories(cat_ids)
        parent_ids = [c.parent_id for c in cats.values() if c.parent_id and c.parent_id not in cats]
        cats.update(await self.repo.categories(parent_ids))

        totals: dict[TransactionType, Decimal] = defaultdict(lambda: Decimal(0))
        by_cat: dict[uuid.UUID | None, tuple[Decimal, int]] = {}
        by_freq: dict[RecurrenceFrequency, tuple[Decimal, int]] = {}
        summaries: list[RecurringRuleSummary] = []
        for r in rules:
            monthly = r.amount * MONTHLY_FACTOR[r.frequency] / r.interval_count
            totals[r.type] += monthly
            summaries.append(
                RecurringRuleSummary(
                    recurring_id=r.id,
                    name=r.name,
                    type=r.type,
                    frequency=r.frequency,
                    interval_count=r.interval_count,
                    amount=r.amount,
                    monthly_amount=monthly,
                    category_id=r.category_id,
                    next_due_date=r.next_due_date,
                )
            )
            if r.type is not TransactionType.EXPENSE:
                continue
            cat = cats.get(r.category_id) if r.category_id else None
            top = cat.parent_id if cat is not None and cat.parent_id else r.category_id
            amount, n = by_cat.get(top, (Decimal(0), 0))
            by_cat[top] = (amount + monthly, n + 1)
            amount, n = by_freq.get(r.frequency, (Decimal(0), 0))
            by_freq[r.frequency] = (amount + monthly, n + 1)

        expense_total = money(totals[TransactionType.EXPENSE])
        categories: list[RecurringCategory] = []
        for cid, (amount, n) in by_cat.items():
            c = cats.get(cid) if cid else None
            categories.append(
                RecurringCategory(
                    category_id=cid,
                    name=c.name if c else UNCATEGORIZED,
                    icon=c.icon if c else None,
                    color=c.color if c else None,
                    monthly_amount=money(amount),
                    rule_count=n,
                    percent=pct(money(amount), expense_total),
                )
            )
        categories.sort(key=lambda c: (-c.monthly_amount, c.name.lower()))
        summaries.sort(key=lambda s: (-s.monthly_amount, s.name.lower()))
        return RecurringSummaryReport(
            meta=ctx.meta(),
            active_rules=len(rules),
            monthly_expense=expense_total,
            monthly_income=money(totals[TransactionType.INCOME]),
            monthly_transfer=money(totals[TransactionType.TRANSFER]),
            by_category=categories,
            by_frequency=[
                RecurringFrequency(frequency=f, monthly_amount=money(a), rule_count=n)
                for f, (a, n) in sorted(by_freq.items(), key=lambda kv: -kv[1][0])
            ],
            rules=summaries,
        )

    async def upi(self, ctx: ReportContext) -> UpiReport:
        lo, hi = ctx.utc
        attempts = self.repo.upi_attempts_where(ctx.scope, ctx.currency, lo, hi)
        by_status = await self.repo.upi_by_status(attempts)
        payees = await self.repo.upi_top_payees(attempts)
        rows = await self.repo.category_totals(
            [*ctx.tx_where(dated=False), self.repo.linked_to_attempts(attempts)]
        )
        rows = [r for r in rows if r.amount != 0]
        spend = sum((r.amount for r in rows), ZERO)
        pending = await self.repo.count_pending_upi(ctx.pending_upi_where())

        count = sum(n for _, n, _ in by_status)
        status_map = {s: (n, a) for s, n, a in by_status}
        ok_n, ok_amount = status_map.get(UpiAttemptStatus.SUCCESS_REPORTED, (0, ZERO))
        return UpiReport(
            meta=ctx.meta(),
            attempts=count,
            attempted_amount=sum((a for _, _, a in by_status), ZERO),
            success_reported=ok_n,
            success_reported_amount=ok_amount,
            success_reported_rate=pct(Decimal(ok_n), Decimal(count)),
            open_attempts=sum(status_map.get(s, (0, ZERO))[0] for s in OPEN_UPI_STATUSES),
            pending_confirmation=pending,
            confirmed_spend=spend,
            by_status=[
                UpiStatusCount(status=s, count=status_map[s][0], amount=status_map[s][1])
                for s in UpiAttemptStatus
                if s in status_map
            ],
            top_payees=[
                UpiPayee(payee_vpa=v, payee_name=n, count=c, amount=a) for v, n, c, a in payees
            ],
            by_category=[slice_of(r, spend) for r in rows],
        )

    # ------------------------------------------------------------------ dispatch + CSV

    async def run(
        self, name: ReportName, user: User, params: ReportParams, now: datetime | None = None
    ) -> Any:
        """Build report ``name`` (used by the CSV export)."""
        if name == "account-balances":
            return await self.account_balances(user, params.account_id, params.days, now)
        monthly = {"income-vs-expense", "personal-vs-family", "savings-trend"}
        months = params.months if name in monthly else None
        if months is not None and not 1 <= months <= MAX_MONTHS:
            raise ValidationFailedError(
                f"months must be between 1 and {MAX_MONTHS}", code="invalid_months"
            )
        ctx = await self.context(user, params, default_months=months, now=now)
        if name == "spending":
            return await self.spending(ctx, params.granularity)
        if name == "income-vs-expense":
            return await self.income_vs_expense(ctx)
        if name == "categories":
            return await self.categories(ctx, params.kind, params.parent_id)
        if name == "members":
            return await self.members(ctx)
        if name == "personal-vs-family":
            return await self.personal_vs_family(ctx)
        if name == "payment-methods":
            return await self.payment_methods(ctx)
        if name == "savings-trend":
            return await self.savings_trend(ctx)
        if name == "budget-performance":
            return await self.budget_performance(ctx, params.periods, now)
        if name == "recurring-summary":
            return await self.recurring_summary(ctx)
        return await self.upi(ctx)


def csv_table(report: Any) -> tuple[list[str], list[list[Any]]]:
    """Header + rows for any report model (one row per item; currency on every row)."""
    cur = report.meta.currency
    if isinstance(report, SpendingReport):
        return ["period", "start_date", "end_date", "amount", "currency"], [
            [i.period, i.start_date, i.end_date, i.amount, cur] for i in report.items
        ]
    if isinstance(report, IncomeExpenseReport):
        return ["month", "start_date", "end_date", "income", "expense", "net", "currency"], [
            [i.month, i.start_date, i.end_date, i.income, i.expense, i.net, cur]
            for i in report.items
        ]
    if isinstance(report, CategoryReport):
        return ["category_id", "category", "amount", "count", "percent", "currency"], [
            [i.category_id, i.name, i.amount, i.count, i.percent, cur] for i in report.items
        ]
    if isinstance(report, MembersReport):
        header = ["user_id", "member", "active", "expense", "income", "count", "percent"]
        return [*header, "currency"], [
            [
                i.user_id,
                i.full_name,
                i.is_active_member,
                i.expense,
                i.income,
                i.count,
                i.percent,
                cur,
            ]
            for i in report.items
        ]
    if isinstance(report, PersonalVsFamilyReport):
        return ["month", "start_date", "end_date", "personal", "family", "currency"], [
            [i.month, i.start_date, i.end_date, i.personal, i.family, cur] for i in report.items
        ]
    if isinstance(report, PaymentMethodsReport):
        return ["payment_method", "amount", "count", "percent", "currency"], [
            [i.payment_method, i.amount, i.count, i.percent, cur] for i in report.items
        ]
    if isinstance(report, AccountBalanceReport):
        return ["date", "account", "balance", "change", "currency"], [
            [i.date, report.name, i.balance, i.change, cur] for i in report.items
        ]
    if isinstance(report, SavingsTrendReport):
        header = ["month", "start_date", "end_date", "deposits", "withdrawals", "net", "balance"]
        return [*header, "currency"], [
            [i.month, i.start_date, i.end_date, i.deposits, i.withdrawals, i.net, i.balance, cur]
            for i in report.items
        ]
    if isinstance(report, BudgetPerformanceReport):
        header = ["budget_id", "budget", "period_key", "start_date", "end_date", "limit", "spent"]
        rows: list[list[Any]] = []
        for b in report.budgets:
            for i in b.items:
                rows.append(
                    [
                        b.budget_id,
                        b.name,
                        i.period_key,
                        i.start_date,
                        i.end_date,
                        i.limit,
                        i.spent,
                        i.remaining,
                        i.percent,
                        i.status,
                        b.currency,
                    ]
                )
        return [*header, "remaining", "percent", "status", "currency"], rows
    if isinstance(report, RecurringSummaryReport):
        header = ["recurring_id", "name", "type", "frequency", "interval_count", "amount"]
        return [*header, "monthly_amount", "next_due_date", "currency"], [
            [
                r.recurring_id,
                r.name,
                r.type.value,
                r.frequency.value,
                r.interval_count,
                r.amount,
                r.monthly_amount,
                r.next_due_date,
                cur,
            ]
            for r in report.rules
        ]
    if isinstance(report, UpiReport):
        rows = [["status", s.status.value, None, s.count, s.amount, cur] for s in report.by_status]
        rows += [
            ["payee", p.payee_vpa, p.payee_name, p.count, p.amount, cur] for p in report.top_payees
        ]
        rows += [
            ["category", i.category_id, i.name, i.count, i.amount, cur] for i in report.by_category
        ]
        return ["section", "key", "name", "count", "amount", "currency"], rows
    raise TypeError(f"no CSV layout for {type(report).__name__}")  # pragma: no cover
