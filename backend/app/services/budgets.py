"""Budgets (spec §8): CRUD, period math, live spending, history, dashboard overview, alerts.

**Periods.** All boundaries are computed in the *budget owner's* timezone (``users.timezone``)
and converted to a half-open UTC range ``[start, end)`` on ``transactions.occurred_at``:

* ``monthly`` — calendar months; ``weekly`` — Monday..Sunday weeks. Both recur from the period
  containing ``start_date`` and, if ``end_date`` is set, stop after the period containing it.
* ``custom`` — exactly ``start_date``..``end_date`` (inclusive), once.

**Spending** is computed at read time (never stored), so edits, deletes, restores and
re-categorisation of transactions are reflected immediately. It is
``SUM(ledger.expense_amount())`` over ``ledger.counted()`` rows (expenses - refunds; transfers,
income, adjustments, pending/failed/cancelled and deleted rows never count) in the budget's
scope: a personal budget sees its owner's *personal* rows; a family budget sees that family's
rows; a member budget only rows created by ``member_user_id``. If the budget has category
limits, its overall ``spent`` only counts those categories (each including its subcategories);
without category limits it counts all spending in scope.

**Carry-forward** (weekly/monthly only): ``carried = amount - spent`` of the *immediately
previous* period (not compounded further back). Unspent money carries as a positive number;
overspending carries as a negative number and reduces the current limit. The first period never
has a carry. Category limits never carry. ``limit = amount + carried``.

**Status:** ``exceeded`` when spent > limit; ``warning`` when percent ≥ 75; else ``ok``.

**Alerts** (:meth:`BudgetService.evaluate_alerts`, run every 10 min by the worker): for every
active budget in its current period, each crossed threshold (percent ≥ threshold) produces one
``budget_alert`` notification with dedupe key ``budget:<id>:<period_key>:<threshold>``, and
spent > limit produces ``budget:<id>:<period_key>:exceeded`` — so each fires once per period.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationFailedError
from app.core.permissions import (
    FamilyPermission,
    list_user_family_roles,
    require_family_access,
    role_has,
)
from app.core.security import utcnow
from app.models.enums import BudgetPeriod, FamilyRole, NotificationCategory, RecordScope
from app.models.family import Family
from app.models.notification import Notification
from app.models.planning import Budget, BudgetCategory
from app.models.user import User
from app.repositories.budgets import BudgetRepository, SpendScope
from app.repositories.system import AuditRepository
from app.schemas.budgets import (
    BudgetCategoryOut,
    BudgetCreate,
    BudgetDetailOut,
    BudgetHistoryItem,
    BudgetHistoryOut,
    BudgetOut,
    BudgetOverviewOut,
    BudgetStatus,
    BudgetUpdate,
    BudgetWarningOut,
    CategoryStatusOut,
    MonthlySummaryOut,
    PeriodState,
    PeriodStatusOut,
)
from app.services.auth import ClientInfo
from app.services.notify import notify

ZERO = Decimal("0.00")
WARNING_PERCENT = Decimal("75")
MAX_HISTORY_PERIODS = 24


# --- period math -------------------------------------------------------------------------


def zone(name: str | None) -> ZoneInfo:
    """The user's timezone; unknown/invalid names fall back to UTC."""
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


@dataclass(frozen=True)
class Period:
    key: str
    start: date  # inclusive, local date
    end: date  # inclusive, local date

    def utc_range(self, tz: ZoneInfo) -> tuple[datetime, datetime]:
        """``[local midnight of start, local midnight after end)`` in UTC."""
        start = datetime.combine(self.start, time.min, tzinfo=tz).astimezone(UTC)
        end = datetime.combine(self.end + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)
        return start, end

    @property
    def length_days(self) -> int:
        return (self.end - self.start).days + 1


def _month_period(day: date) -> Period:
    start = day.replace(day=1)
    nxt = date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
    return Period(f"{start.year:04d}-{start.month:02d}", start, nxt - timedelta(days=1))


def _week_period(day: date) -> Period:
    start = day - timedelta(days=day.weekday())
    iso = start.isocalendar()
    return Period(f"{iso.year:04d}-W{iso.week:02d}", start, start + timedelta(days=6))


def period_containing(
    period: BudgetPeriod, day: date, *, start_date: date | None = None, end_date: date | None = None
) -> Period:
    """The period of type ``period`` containing ``day`` (custom: ``start_date..end_date``)."""
    if period is BudgetPeriod.MONTHLY:
        return _month_period(day)
    if period is BudgetPeriod.WEEKLY:
        return _week_period(day)
    start = start_date or day
    end = end_date or start
    return Period(f"custom-{start.isoformat()}", start, end)


def previous_period(period: BudgetPeriod, current: Period) -> Period | None:
    if period is BudgetPeriod.CUSTOM:
        return None
    return period_containing(period, current.start - timedelta(days=1))


def first_period(budget: Budget) -> Period:
    return period_containing(
        budget.period, budget.start_date, start_date=budget.start_date, end_date=budget.end_date
    )


def current_period(budget: Budget, today: date) -> tuple[Period, PeriodState]:
    """The period to show for ``today`` and whether it is upcoming / current / ended."""
    first = first_period(budget)
    if budget.period is BudgetPeriod.CUSTOM:
        if today < first.start:
            return first, "upcoming"
        return first, "ended" if today > first.end else "current"
    if today < first.start:
        return first, "upcoming"
    if budget.end_date is not None:
        last = period_containing(budget.period, budget.end_date)
        if today > last.end:
            return last, "ended"
    return period_containing(budget.period, today), "current"


def days_left(period: Period, state: PeriodState, today: date) -> int:
    if state == "ended":
        return 0
    if state == "upcoming":
        return period.length_days
    return (period.end - today).days + 1


# --- status math -------------------------------------------------------------------------


def percent_of(spent: Decimal, limit: Decimal) -> Decimal:
    """Spent as % of limit (exact). A limit ≤ 0 counts as fully used (100)."""
    if limit <= 0:
        return Decimal(100)
    return spent * 100 / limit


def round_percent(value: Decimal) -> float:
    return float(max(value, Decimal(0)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def status_of(spent: Decimal, limit: Decimal) -> BudgetStatus:
    if spent > limit:
        return "exceeded"
    if percent_of(spent, limit) >= WARNING_PERCENT:
        return "warning"
    return "ok"


def crossed_thresholds(thresholds: list[int], spent: Decimal, limit: Decimal) -> list[int]:
    """Thresholds whose percentage has been reached (``spent/limit ≥ t%``)."""
    if limit <= 0:
        return [t for t in thresholds if t <= 100 or spent > limit]
    return [t for t in thresholds if spent * 100 >= limit * t]


def _fmt_money(value: Decimal, currency: str) -> str:
    symbol = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}.get(currency)
    text = f"{value.quantize(Decimal('0.01')):,.2f}"
    return f"{symbol}{text}" if symbol else f"{currency} {text}"


def _period_label(period: BudgetPeriod, p: Period) -> str:
    if period is BudgetPeriod.MONTHLY:
        return p.start.strftime("%B %Y")
    if period is BudgetPeriod.WEEKLY:
        return f"week of {p.start.day} {p.start.strftime('%b')}"
    return f"{p.start.day} {p.start.strftime('%b')} - {p.end.day} {p.end.strftime('%b %Y')}"


@dataclass(frozen=True)
class _Status:
    period: Period
    state: PeriodState
    limit: Decimal
    carried: Decimal
    spent: Decimal
    days_left: int

    @property
    def remaining(self) -> Decimal:
        return self.limit - self.spent

    @property
    def status(self) -> BudgetStatus:
        return status_of(self.spent, self.limit)

    def out(self) -> PeriodStatusOut:
        return PeriodStatusOut(**self._fields())

    def history_item(self, is_current: bool) -> BudgetHistoryItem:
        return BudgetHistoryItem(**self._fields(), is_current=is_current)

    def _fields(self) -> dict[str, Any]:
        return {
            "period_key": self.period.key,
            "start_date": self.period.start,
            "end_date": self.period.end,
            "state": self.state,
            "limit": self.limit,
            "carried": self.carried,
            "spent": self.spent,
            "remaining": self.remaining,
            "percent": round_percent(percent_of(self.spent, self.limit)),
            "status": self.status,
            "days_left": self.days_left,
        }


# --- service -----------------------------------------------------------------------------


class BudgetService:
    def __init__(self, session: AsyncSession) -> None:
        self.db = session
        self.repo = BudgetRepository(session)
        self.audit = AuditRepository(session)

    # ------------------------------------------------------------------ computation

    async def _compute(
        self,
        budget: Budget,
        tz: ZoneInfo,
        period: Period,
        state: PeriodState,
        today: date,
        category_ids: list[uuid.UUID],
    ) -> _Status:
        scope = SpendScope.of(budget)
        cats = category_ids or None
        spent = await self.repo.spent(scope, *period.utc_range(tz), cats)
        carried = ZERO
        prev = previous_period(budget.period, period) if budget.carry_forward else None
        if prev is not None and prev.start >= first_period(budget).start:
            carried = budget.amount - await self.repo.spent(scope, *prev.utc_range(tz), cats)
        return _Status(
            period=period,
            state=state,
            limit=budget.amount + carried,
            carried=carried,
            spent=spent,
            days_left=days_left(period, state, today),
        )

    async def _current(
        self, budget: Budget, tz: ZoneInfo, now: datetime, category_ids: list[uuid.UUID]
    ) -> _Status:
        today = now.astimezone(tz).date()
        period, state = current_period(budget, today)
        return await self._compute(budget, tz, period, state, today, category_ids)

    # ------------------------------------------------------------------ helpers

    async def _visible(
        self, user: User, budget_id: uuid.UUID, *, for_update: bool = False
    ) -> Budget:
        budget = await self.repo.get_visible(user.id, budget_id, for_update=for_update)
        if budget is None:
            raise NotFoundError("Budget not found")
        return budget

    async def _require_write(self, user: User, budget: Budget) -> None:
        if budget.scope is RecordScope.PERSONAL:
            if budget.owner_user_id != user.id:  # pragma: no cover - not visible anyway
                raise NotFoundError("Budget not found")
            return
        if budget.family_id is None:  # pragma: no cover - enforced by ck_*_scope_family
            raise NotFoundError()
        await require_family_access(
            self.db, user, budget.family_id, FamilyPermission.MANAGE_BUDGETS
        )

    @staticmethod
    def _can_edit(user: User, budget: Budget, roles: dict[uuid.UUID, FamilyRole]) -> bool:
        if budget.scope is RecordScope.PERSONAL:
            return budget.owner_user_id == user.id
        role = roles.get(budget.family_id) if budget.family_id else None
        return role is not None and role_has(role, FamilyPermission.MANAGE_BUDGETS)

    async def _validate_member(self, family_id: uuid.UUID | None, member_id: uuid.UUID) -> None:
        if family_id is None:
            raise ValidationFailedError("member_user_id is only allowed on family budgets")
        if not await self.repo.is_active_member(family_id, member_id):
            raise ValidationFailedError(
                "member_user_id must be an active member of the family",
                code="invalid_member",
            )

    async def _validate_categories(
        self, ids: list[uuid.UUID], owner_user_id: uuid.UUID, family_id: uuid.UUID | None
    ) -> None:
        usable = await self.repo.usable_expense_categories(
            ids, owner_user_id=owner_user_id, family_id=family_id
        )
        missing = [str(i) for i in ids if i not in usable]
        if missing:
            raise ValidationFailedError(
                "Unknown category (must be a live expense category usable in this scope)",
                code="invalid_category",
                details={"category_ids": missing},
            )

    def _audit(
        self, action: str, user: User, budget: Budget, client: ClientInfo, **details: object
    ) -> None:
        self.audit.record(
            action,
            actor_user_id=user.id,
            entity_type="budget",
            entity_id=budget.id,
            family_id=budget.family_id,
            ip_address=client.ip,
            user_agent=client.user_agent,
            details=dict(details),
        )

    async def _out(
        self,
        user: User,
        budget: Budget,
        now: datetime,
        roles: dict[uuid.UUID, FamilyRole],
        categories: list[tuple[BudgetCategory, str]],
        tz: ZoneInfo,
        *,
        detail: bool = False,
    ) -> BudgetDetailOut | BudgetOut:
        cat_ids = [bc.category_id for bc, _ in categories]
        status = await self._current(budget, tz, now, cat_ids)
        fields: dict[str, Any] = {
            "id": budget.id,
            "scope": budget.scope,
            "family_id": budget.family_id,
            "owner_user_id": budget.owner_user_id,
            "member_user_id": budget.member_user_id,
            "name": budget.name,
            "period": budget.period,
            "start_date": budget.start_date,
            "end_date": budget.end_date,
            "amount": budget.amount,
            "currency": budget.currency,
            "carry_forward": budget.carry_forward,
            "alert_thresholds": sorted(budget.alert_thresholds),
            "is_active": budget.is_active,
            "categories": [
                BudgetCategoryOut(category_id=bc.category_id, name=name, amount=bc.amount)
                for bc, name in categories
            ],
            "can_edit": self._can_edit(user, budget, roles),
            "created_at": budget.created_at,
            "updated_at": budget.updated_at,
            "current": status.out(),
        }
        if not detail:
            return BudgetOut(**fields)
        spent = await self.repo.spent_by_category(
            SpendScope.of(budget), *status.period.utc_range(tz), cat_ids
        )
        breakdown = []
        for bc, name in categories:
            s = spent[bc.category_id]
            breakdown.append(
                CategoryStatusOut(
                    category_id=bc.category_id,
                    name=name,
                    limit=bc.amount,
                    spent=s,
                    remaining=bc.amount - s,
                    percent=round_percent(percent_of(s, bc.amount)),
                    status=status_of(s, bc.amount),
                )
            )
        return BudgetDetailOut(**fields, category_breakdown=breakdown)

    async def _detail(self, user: User, budget: Budget, now: datetime) -> BudgetDetailOut:
        roles = await list_user_family_roles(self.db, user.id)
        cats = (await self.repo.list_categories([budget.id]))[budget.id]
        tz = zone(await self.repo.owner_timezone(budget.owner_user_id))
        out = await self._out(user, budget, now, roles, cats, tz, detail=True)
        if not isinstance(out, BudgetDetailOut):  # pragma: no cover - detail=True
            raise TypeError("expected a detail response")
        return out

    # ------------------------------------------------------------------ CRUD

    async def create(
        self, user: User, data: BudgetCreate, client: ClientInfo, now: datetime | None = None
    ) -> BudgetDetailOut:
        now = now or utcnow()
        currency = data.currency or "INR"
        if data.scope is RecordScope.FAMILY:
            if data.family_id is None:  # pragma: no cover - enforced by ck_*_scope_family
                raise NotFoundError()
            await require_family_access(
                self.db, user, data.family_id, FamilyPermission.MANAGE_BUDGETS
            )
            if data.currency is None:
                family = await self.db.get(Family, data.family_id)
                currency = family.currency if family is not None else "INR"
        if data.member_user_id is not None:
            await self._validate_member(data.family_id, data.member_user_id)
        await self._validate_categories(
            [c.category_id for c in data.categories], user.id, data.family_id
        )
        start = data.start_date or now.astimezone(zone(user.timezone)).date()
        if data.end_date is not None and data.end_date < start:
            raise ValidationFailedError("end_date must be on or after start_date")
        budget = Budget(
            owner_user_id=user.id,
            scope=data.scope,
            family_id=data.family_id,
            member_user_id=data.member_user_id,
            name=data.name,
            period=data.period,
            start_date=start,
            end_date=data.end_date,
            amount=data.amount,
            currency=currency,
            carry_forward=data.carry_forward,
            alert_thresholds=list(data.alert_thresholds),
            is_active=data.is_active,
        )
        self.repo.add(budget)
        await self.db.flush()
        await self.repo.replace_categories(
            budget.id, [(c.category_id, c.amount) for c in data.categories]
        )
        self._audit("budget.created", user, budget, client, name=budget.name)
        await self.db.commit()
        await self.db.refresh(budget)
        return await self._detail(user, budget, now)

    async def list_budgets(
        self,
        user: User,
        family_id: uuid.UUID | None,
        *,
        is_active: bool | None = None,
        now: datetime | None = None,
    ) -> list[BudgetOut]:
        now = now or utcnow()
        if family_id is not None:
            await require_family_access(self.db, user, family_id)
        budgets = await self.repo.list_for_scope(user.id, family_id, is_active=is_active)
        roles = await list_user_family_roles(self.db, user.id)
        cats = await self.repo.list_categories([b.id for b in budgets])
        tzs = await self.repo.owner_timezones(b.owner_user_id for b in budgets)
        out: list[BudgetOut] = []
        for b in budgets:
            tz = zone(tzs.get(b.owner_user_id))
            out.append(await self._out(user, b, now, roles, cats[b.id], tz))
        return out

    async def get_budget(
        self, user: User, budget_id: uuid.UUID, now: datetime | None = None
    ) -> BudgetDetailOut:
        budget = await self._visible(user, budget_id)
        return await self._detail(user, budget, now or utcnow())

    async def update(
        self,
        user: User,
        budget_id: uuid.UUID,
        data: BudgetUpdate,
        client: ClientInfo,
        now: datetime | None = None,
    ) -> BudgetDetailOut:
        now = now or utcnow()
        budget = await self._visible(user, budget_id, for_update=True)
        await self._require_write(user, budget)
        changes = data.model_dump(exclude_unset=True)
        categories = changes.pop("categories", None)

        period = changes.get("period", budget.period)
        start = changes.get("start_date", budget.start_date)
        end = changes.get("end_date", budget.end_date)
        if period is BudgetPeriod.CUSTOM and end is None:
            raise ValidationFailedError("custom budgets need an end_date")
        if end is not None and end < start:
            raise ValidationFailedError("end_date must be on or after start_date")
        if changes.get("member_user_id") is not None:
            await self._validate_member(budget.family_id, changes["member_user_id"])
        if data.categories is not None:
            await self._validate_categories(
                [c.category_id for c in data.categories], budget.owner_user_id, budget.family_id
            )

        for field, value in changes.items():
            setattr(budget, field, list(value) if field == "alert_thresholds" else value)
        if data.categories is not None:
            await self.repo.replace_categories(
                budget.id, [(c.category_id, c.amount) for c in data.categories]
            )
        fields = sorted(changes) + (["categories"] if categories is not None else [])
        if fields:
            self._audit("budget.updated", user, budget, client, fields=fields)
            await self.db.commit()
            await self.db.refresh(budget)
        return await self._detail(user, budget, now)

    async def delete(self, user: User, budget_id: uuid.UUID, client: ClientInfo) -> None:
        budget = await self._visible(user, budget_id, for_update=True)
        await self._require_write(user, budget)
        budget.deleted_at = utcnow()
        self._audit("budget.deleted", user, budget, client, name=budget.name)
        await self.db.commit()

    # ------------------------------------------------------------------ history / overview

    async def history(
        self, user: User, budget_id: uuid.UUID, periods: int = 6, now: datetime | None = None
    ) -> BudgetHistoryOut:
        """The last ``periods`` periods up to and including the current (or last/upcoming)
        one, oldest first. Uses the budget's *current* amount and category set for every
        period (budgets are not versioned)."""
        now = now or utcnow()
        budget = await self._visible(user, budget_id)
        cats = (await self.repo.list_categories([budget.id]))[budget.id]
        cat_ids = [bc.category_id for bc, _ in cats]
        tz = zone(await self.repo.owner_timezone(budget.owner_user_id))
        today = now.astimezone(tz).date()
        current, state = current_period(budget, today)
        first_start = first_period(budget).start
        chain: list[tuple[Period, PeriodState]] = [(current, state)]
        while len(chain) < max(1, min(periods, MAX_HISTORY_PERIODS)):
            prev = previous_period(budget.period, chain[-1][0])
            if prev is None or prev.start < first_start:
                break
            chain.append((prev, "ended"))
        items = []
        for p, st in reversed(chain):
            status = await self._compute(budget, tz, p, st, today, cat_ids)
            items.append(status.history_item(is_current=p == current))
        return BudgetHistoryOut(budget_id=budget.id, period=budget.period, items=items)

    async def overview(
        self, user: User, family_id: uuid.UUID | None, now: datetime | None = None
    ) -> BudgetOverviewOut:
        """Dashboard numbers: this calendar month (caller's timezone) total monthly budget vs
        spent, plus every active budget currently at warning/exceeded."""
        now = now or utcnow()
        if family_id is not None:
            await require_family_access(self.db, user, family_id)
        budgets = await self.repo.list_for_scope(user.id, family_id, is_active=True)
        cats = await self.repo.list_categories([b.id for b in budgets])
        tzs = await self.repo.owner_timezones(b.owner_user_id for b in budgets)

        statuses: list[tuple[Budget, _Status]] = []
        for b in budgets:
            ids = [bc.category_id for bc, _ in cats[b.id]]
            statuses.append((b, await self._current(b, zone(tzs.get(b.owner_user_id)), now, ids)))

        warnings = [
            BudgetWarningOut(
                budget_id=b.id,
                name=b.name,
                period=b.period,
                member_user_id=b.member_user_id,
                limit=s.limit,
                spent=s.spent,
                remaining=s.remaining,
                percent=round_percent(percent_of(s.spent, s.limit)),
                status=s.status,
            )
            for b, s in statuses
            if s.state == "current" and s.status != "ok"
        ]
        warnings.sort(key=lambda w: (-w.percent, w.name))

        monthly = [
            (b, s)
            for b, s in statuses
            if b.period is BudgetPeriod.MONTHLY
            and b.member_user_id is None
            and s.state == "current"
        ]
        overall = [(b, s) for b, s in monthly if not cats[b.id]]
        month = _month_period(now.astimezone(zone(user.timezone)).date())
        month_range = month.utc_range(zone(user.timezone))
        # Spending in the budgets' currency when they share one (never a mixed-currency sum).
        currencies = {b.currency for b, _ in (overall or monthly)}
        currency = currencies.pop() if len(currencies) == 1 else None
        scope = (
            SpendScope(RecordScope.FAMILY, user.id, family_id, currency=currency)
            if family_id is not None
            else SpendScope(RecordScope.PERSONAL, user.id, None, currency=currency)
        )
        if overall:
            basis = "overall"
            chosen = overall
            spent = await self.repo.spent(scope, *month_range)
        elif monthly:
            basis = "categories"
            chosen = monthly
            union = list({bc.category_id for b, _ in monthly for bc, _ in cats[b.id]})
            spent = await self.repo.spent(scope, *month_range, union)
        else:
            basis = "none"
            chosen = []
            spent = await self.repo.spent(scope, *month_range)
        total = sum((s.limit for _, s in chosen), ZERO)
        if basis == "none":
            summary = MonthlySummaryOut(
                period_key=month.key,
                start_date=month.start,
                end_date=month.end,
                basis="none",
                budget_count=0,
                total_budget=ZERO,
                spent=spent,
                remaining=ZERO,
                percent=0.0,
                status="ok",
            )
        else:
            summary = MonthlySummaryOut(
                period_key=month.key,
                start_date=month.start,
                end_date=month.end,
                basis="overall" if basis == "overall" else "categories",
                budget_count=len(chosen),
                total_budget=total,
                spent=spent,
                remaining=total - spent,
                percent=round_percent(percent_of(spent, total)),
                status=status_of(spent, total),
            )
        return BudgetOverviewOut(
            scope=RecordScope.FAMILY if family_id is not None else RecordScope.PERSONAL,
            family_id=family_id,
            month=summary,
            warnings=warnings,
        )

    # ------------------------------------------------------------------ alerts

    @staticmethod
    async def evaluate_alerts(db: AsyncSession, now: datetime) -> int:
        """Create budget-alert notifications for every threshold crossed in each active
        budget's current period (and ``exceeded``). Idempotent via dedupe keys; commits.
        Returns the number of notifications created."""
        svc = BudgetService(db)
        repo = svc.repo
        rows = await repo.list_active_for_alerts()
        cats = await repo.list_categories([b.id for b, _ in rows])
        created = 0
        for budget, tz_name in rows:
            tz = zone(tz_name)
            today = now.astimezone(tz).date()
            period, state = current_period(budget, today)
            if state != "current":
                continue
            ids = [bc.category_id for bc, _ in cats[budget.id]]
            status = await svc._compute(budget, tz, period, state, today, ids)
            events: list[tuple[str, int | None]] = [
                (str(t), t)
                for t in crossed_thresholds(
                    sorted(budget.alert_thresholds), status.spent, status.limit
                )
            ]
            if status.spent > status.limit:
                events.append(("exceeded", None))
            if not events:
                continue
            prefix = f"budget:{budget.id}:{period.key}:"
            recipients = await repo.alert_recipients(budget)
            rows_seen = await db.execute(
                select(Notification.user_id, Notification.dedupe_key).where(
                    Notification.user_id.in_(recipients),
                    Notification.dedupe_key.in_([prefix + key for key, _ in events]),
                )
            )
            existing = {(uid, key) for uid, key in rows_seen}
            label = _period_label(budget.period, period)
            spent_txt = _fmt_money(status.spent, budget.currency)
            limit_txt = _fmt_money(status.limit, budget.currency)
            for key, threshold in events:
                if threshold is None:
                    title = f"{budget.name}: budget exceeded"
                    body = (
                        f"{spent_txt} spent of {limit_txt} ({label}) — over by "
                        f"{_fmt_money(-status.remaining, budget.currency)}."
                    )
                else:
                    title = f"{budget.name}: {threshold}% of budget used"
                    body = (
                        f"{spent_txt} spent of {limit_txt} ({label}). "
                        f"{_fmt_money(max(status.remaining, ZERO), budget.currency)} left."
                    )
                data = {
                    "budget_id": str(budget.id),
                    "period_key": period.key,
                    "threshold": key,
                    "percent": round_percent(percent_of(status.spent, status.limit)),
                }
                for uid in recipients:
                    if (uid, prefix + key) in existing:
                        continue
                    if await notify(
                        db,
                        user_id=uid,
                        category=NotificationCategory.BUDGET_ALERT,
                        title=title,
                        body=body,
                        dedupe_key=prefix + key,
                        data=data,
                        family_id=budget.family_id,
                    ):
                        created += 1
        await db.commit()
        return created
