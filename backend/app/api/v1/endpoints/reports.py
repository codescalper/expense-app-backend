"""Report endpoints (/api/v1/reports) and their CSV export.

Every report takes ``family_id`` (absent → my personal rows; present → that family's rows,
404 unless I'm an active member), ``preset`` / ``from`` / ``to`` (local dates in my timezone)
and ``currency``. See :mod:`app.services.reports` for the rules.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentPrincipal, DbSession, user_rate_limit
from app.schemas.reports import (
    AccountBalanceReport,
    BudgetPerformanceReport,
    CategoryReport,
    CategoryReportKind,
    Granularity,
    IncomeExpenseReport,
    MembersReport,
    PaymentMethodsReport,
    PersonalVsFamilyReport,
    Preset,
    RecurringSummaryReport,
    ReportName,
    SavingsTrendReport,
    SpendingReport,
    UpiReport,
)
from app.services.reports import (
    MAX_BALANCE_DAYS,
    MAX_BUDGET_PERIODS,
    MAX_MONTHS,
    ReportParams,
    ReportService,
    csv_table,
    iter_csv,
)

router = APIRouter(
    prefix="/reports",
    tags=["reports"],
    dependencies=[Depends(user_rate_limit("analytics"))],
)


def get_report_service(db: DbSession) -> ReportService:
    return ReportService(db)


ReportSvc = Annotated[ReportService, Depends(get_report_service)]


def report_params(
    family_id: Annotated[
        uuid.UUID | None,
        Query(description="Omit for my personal rows; a family id for that family"),
    ] = None,
    preset: Annotated[Preset | None, Query(description="Default this_month")] = None,
    date_from: Annotated[date | None, Query(alias="from", description="YYYY-MM-DD")] = None,
    date_to: Annotated[date | None, Query(alias="to", description="YYYY-MM-DD, inclusive")] = None,
    currency: Annotated[str | None, Query(pattern=r"^[A-Za-z]{3}$")] = None,
) -> ReportParams:
    return ReportParams(
        family_id=family_id,
        preset=preset,
        date_from=date_from,
        date_to=date_to,
        currency=currency.upper() if currency else None,
    )


Params = Annotated[ReportParams, Depends(report_params)]
MonthsQuery = Annotated[
    int, Query(ge=1, le=MAX_MONTHS, description="Used when no preset/from/to is given")
]


@router.get("/spending")
async def spending_report(
    principal: CurrentPrincipal,
    svc: ReportSvc,
    params: Params,
    granularity: Granularity = "day",
) -> SpendingReport:
    """Spending (expenses - refunds) per day/week/month/year bucket, zero-filled."""
    p = dataclasses.replace(params, granularity=granularity)
    out: SpendingReport = await svc.run("spending", principal.user, p)
    return out


@router.get("/income-vs-expense")
async def income_vs_expense_report(
    principal: CurrentPrincipal, svc: ReportSvc, params: Params, months: MonthsQuery = 12
) -> IncomeExpenseReport:
    out: IncomeExpenseReport = await svc.run(
        "income-vs-expense", principal.user, dataclasses.replace(params, months=months)
    )
    return out


@router.get("/categories")
async def categories_report(
    principal: CurrentPrincipal,
    svc: ReportSvc,
    params: Params,
    kind: CategoryReportKind = "expense",
    parent_id: Annotated[
        uuid.UUID | None, Query(description="Drill down into this category's subcategories")
    ] = None,
) -> CategoryReport:
    """Donut data: spending per top-level category (subcategories rolled up)."""
    out: CategoryReport = await svc.run(
        "categories", principal.user, dataclasses.replace(params, kind=kind, parent_id=parent_id)
    )
    return out


@router.get("/members")
async def members_report(
    principal: CurrentPrincipal, svc: ReportSvc, params: Params
) -> MembersReport:
    """Family scope only (422 ``family_scope_required`` without ``family_id``)."""
    out: MembersReport = await svc.run("members", principal.user, params)
    return out


@router.get("/personal-vs-family")
async def personal_vs_family_report(
    principal: CurrentPrincipal, svc: ReportSvc, params: Params, months: MonthsQuery = 6
) -> PersonalVsFamilyReport:
    """Personal scope only: my personal spending vs spending I recorded in my families."""
    out: PersonalVsFamilyReport = await svc.run(
        "personal-vs-family", principal.user, dataclasses.replace(params, months=months)
    )
    return out


@router.get("/payment-methods")
async def payment_methods_report(
    principal: CurrentPrincipal, svc: ReportSvc, params: Params
) -> PaymentMethodsReport:
    out: PaymentMethodsReport = await svc.run("payment-methods", principal.user, params)
    return out


@router.get("/account-balances")
async def account_balances_report(
    principal: CurrentPrincipal,
    svc: ReportSvc,
    account_id: uuid.UUID,
    days: Annotated[int, Query(ge=1, le=MAX_BALANCE_DAYS)] = 90,
) -> AccountBalanceReport:
    """Daily end-of-day balance of one visible account for the last ``days`` days."""
    out: AccountBalanceReport = await svc.run(
        "account-balances", principal.user, ReportParams(account_id=account_id, days=days)
    )
    return out


@router.get("/savings-trend")
async def savings_trend_report(
    principal: CurrentPrincipal, svc: ReportSvc, params: Params, months: MonthsQuery = 12
) -> SavingsTrendReport:
    out: SavingsTrendReport = await svc.run(
        "savings-trend", principal.user, dataclasses.replace(params, months=months)
    )
    return out


@router.get("/budget-performance")
async def budget_performance_report(
    principal: CurrentPrincipal,
    svc: ReportSvc,
    params: Params,
    periods: Annotated[int, Query(ge=1, le=MAX_BUDGET_PERIODS)] = 6,
) -> BudgetPerformanceReport:
    """Limit vs spent for the last ``periods`` periods of every active budget in scope."""
    out: BudgetPerformanceReport = await svc.run(
        "budget-performance", principal.user, dataclasses.replace(params, periods=periods)
    )
    return out


@router.get("/recurring-summary")
async def recurring_summary_report(
    principal: CurrentPrincipal, svc: ReportSvc, params: Params
) -> RecurringSummaryReport:
    """Active recurring rules normalised to a monthly amount."""
    out: RecurringSummaryReport = await svc.run("recurring-summary", principal.user, params)
    return out


@router.get("/upi")
async def upi_report(principal: CurrentPrincipal, svc: ReportSvc, params: Params) -> UpiReport:
    """UPI payment attempts (by ``initiated_at``) and the spending they produced."""
    out: UpiReport = await svc.run("upi", principal.user, params)
    return out


@router.get(
    "/export.csv",
    response_class=StreamingResponse,
    dependencies=[Depends(user_rate_limit("export"))],
)
async def export_report_csv(
    principal: CurrentPrincipal,
    svc: ReportSvc,
    params: Params,
    report: ReportName,
    granularity: Granularity = "day",
    months: Annotated[int | None, Query(ge=1, le=MAX_MONTHS)] = None,
    kind: CategoryReportKind = "expense",
    parent_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    days: Annotated[int, Query(ge=1, le=MAX_BALANCE_DAYS)] = 90,
    periods: Annotated[int, Query(ge=1, le=MAX_BUDGET_PERIODS)] = 6,
) -> StreamingResponse:
    """Any report as CSV (same parameters as the JSON endpoint). Text cells that could be read
    as spreadsheet formulas are prefixed with ``'``."""
    p = dataclasses.replace(
        params,
        granularity=granularity,
        months=months or (6 if report == "personal-vs-family" else 12),
        kind=kind,
        parent_id=parent_id,
        account_id=account_id,
        days=days,
        periods=periods,
    )
    data = await svc.run(report, principal.user, p)
    header, rows = csv_table(data)
    rng = data.meta.range
    filename = f"{report}-{rng.start_date.isoformat()}-{rng.end_date.isoformat()}.csv"
    return StreamingResponse(
        iter_csv(header, rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
