"""Home dashboard (spec §4): every card of the Home screen in one round trip.

Scope/date/currency conventions are those of :mod:`app.services.reports` (default preset
``this_month``). Cards and their sources:

* **balance** — current ledger balance (:func:`app.services.ledger.account_balances`) of the
  *active* accounts visible in the scope (in the report currency). Credit cards are not
  "available" money; they are listed under ``liabilities``.
* **totals** — period income / expense / net savings (:func:`app.services.ledger.totals`).
* **spending_split** — personal view: my personal spending vs spending I recorded in my
  families; family view: family total + per-member breakdown.
* **budget** — :meth:`BudgetService.overview` (this calendar month + warnings).
* **recent_transactions** — last 8 live completed/pending rows in scope (any currency).
* **upcoming_bills** — :meth:`RecurringService.upcoming` for the next 7 days incl. overdue.
* **savings** — top 3 active goals by progress (:meth:`SavingsService.list_goals`).
* **spending_by_category** — period spending per top-level category: top 6 + ``Other``.
* **monthly_trend** — income vs expense for the last 6 calendar months.
* **pending_upi_count** — pending ``upi_intent`` transactions awaiting confirmation (any date).

The number of SQL statements is bounded and independent of the amount of data (only the
budget card grows with the number of active budgets — see ``BudgetService.overview``).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import utcnow
from app.models.enums import AccountType, GoalStatus
from app.models.finance import Transaction
from app.models.user import User
from app.repositories.reports import RecentRow, ReportRepository
from app.schemas.dashboard import (
    AccountBalanceOut,
    BalanceCard,
    DashboardOut,
    PeriodTotals,
    RecentCategory,
    RecentTransaction,
    SavingsCard,
    SpendingSplit,
)
from app.services import ledger
from app.services.budgets import BudgetService
from app.services.recurring import RecurringService
from app.services.reports import (
    ReportContext,
    ReportParams,
    ReportService,
    add_months,
    month_end,
    month_start,
    pct,
    top_with_other,
)
from app.services.savings import SavingsService

ZERO = Decimal("0.00")
RECENT_LIMIT = 8
UPCOMING_DAYS = 7
TOP_GOALS = 3
TOP_CATEGORIES = 6
TREND_MONTHS = 6


def _recent_out(row: RecentRow) -> RecentTransaction:
    tx = row.tx
    category = (
        RecentCategory(
            id=row.category_id,
            name=row.category_name or "",
            icon=row.category_icon,
            color=row.category_color,
        )
        if row.category_id is not None
        else None
    )
    return RecentTransaction(
        id=tx.id,
        type=tx.type,
        status=tx.status,
        source=tx.source,
        amount=tx.amount,
        currency=tx.currency,
        occurred_at=tx.occurred_at,
        description=tx.description,
        merchant=tx.merchant,
        payee_name=row.payee_name,
        account_name=row.account_name,
        category=category,
        created_by=tx.owner_user_id,
        created_by_name=row.created_by_name,
        upi_attempt_id=tx.upi_attempt_id,
    )


class DashboardService:
    def __init__(self, session: AsyncSession) -> None:
        self.db = session
        self.reports = ReportService(session)
        self.repo = ReportRepository(session)

    async def _balance(self, ctx: ReportContext) -> BalanceCard:
        accounts = await self.repo.scope_accounts(ctx.scope, ctx.currency)
        balances = await ledger.account_balances(self.db, [a.id for a in accounts])
        assets: list[AccountBalanceOut] = []
        liabilities: list[AccountBalanceOut] = []
        for a in accounts:
            out = AccountBalanceOut(
                id=a.id, name=a.name, type=a.type, balance=balances.get(a.id, ZERO)
            )
            (liabilities if a.type is AccountType.CREDIT_CARD else assets).append(out)
        available = sum((a.balance for a in assets), ZERO)
        owed = sum((a.balance for a in liabilities), ZERO)
        return BalanceCard(
            available=available,
            liabilities_total=owed,
            net_worth=available + owed,
            accounts=assets,
            liabilities=liabilities,
        )

    async def _split(self, ctx: ReportContext, family_total: Decimal) -> SpendingSplit:
        if ctx.family_id is not None:
            members, _ = await self.reports.member_spending(ctx)
            mine = next((m.expense for m in members if m.user_id == ctx.user.id), ZERO)
            return SpendingSplit(view="family", personal=mine, family=family_total, members=members)
        t = Transaction
        lo, hi = ctx.utc
        dated = and_(t.currency == ctx.currency, t.occurred_at >= lo, t.occurred_at < hi)
        personal, family = await self.repo.spending_split(
            and_(ctx.scope.clause(t), dated), and_(ctx.mine_in_families(), dated)
        )
        return SpendingSplit(view="personal", personal=personal, family=family, members=[])

    async def get(
        self, user: User, params: ReportParams, now: datetime | None = None
    ) -> DashboardOut:
        now = now or utcnow()
        ctx = await self.reports.context(user, params, now=now)

        balance = await self._balance(ctx)
        sums = await ledger.totals(self.db, *ctx.tx_where())
        split = await self._split(ctx, sums["expense"])
        budget = await BudgetService(self.db).overview(user, ctx.family_id, now)
        recent = await self.repo.recent([ctx.scope.clause(Transaction)], RECENT_LIMIT)
        upcoming = await RecurringService(self.db).upcoming(
            user, ctx.family_id, UPCOMING_DAYS, include_overdue=True, now=now
        )
        goals = await SavingsService(self.db).list_goals(
            user, ctx.family_id, GoalStatus.ACTIVE, now
        )
        slices, spent = await self.reports.category_slices(ctx)
        trend_ctx = dataclasses.replace(
            ctx,
            start=add_months(month_start(ctx.today), -(TREND_MONTHS - 1)),
            end=month_end(ctx.today),
        )
        trend = await self.reports.monthly_income_expense(trend_ctx)
        pending_upi = await self.repo.count_pending_upi(ctx.pending_upi_where())

        in_currency = [g for g in goals if g.currency == ctx.currency]
        top_goals = sorted(goals, key=lambda g: (-g.progress_percent, g.created_at))[:TOP_GOALS]
        return DashboardOut(
            meta=ctx.meta(),
            balance=balance,
            totals=PeriodTotals(
                income=sums["income"],
                expense=sums["expense"],
                net_savings=sums["net"],
                savings_rate=pct(sums["net"], sums["income"]),
            ),
            spending_split=split,
            budget=budget,
            recent_transactions=[_recent_out(r) for r in recent],
            upcoming_bills=upcoming,
            savings=SavingsCard(
                active_count=len(goals),
                target_total=sum((g.target_amount for g in in_currency), ZERO),
                saved_total=sum((g.current_amount for g in in_currency), ZERO),
                goals=top_goals,
            ),
            spending_by_category=top_with_other(slices, spent, TOP_CATEGORIES),
            monthly_trend=trend,
            pending_upi_count=pending_upi,
        )
