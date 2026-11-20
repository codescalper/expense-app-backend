"""Read-only aggregation queries for reports and the dashboard.

Every query aggregates in SQL (``GROUP BY``, ``generate_series`` for zero-filled buckets) and
uses the accounting rules from :mod:`app.services.ledger` (``counted()``, ``expense_amount()``,
``income_amount()``, ``source_account_delta()``). Callers pass the visibility/currency/date
conditions as ``where`` clauses (built from :class:`~app.core.permissions.ScopeFilter`), so no
method here can widen what a user sees.

Timezone bucketing uses ``timezone(<IANA name>, occurred_at)`` (= ``occurred_at AT TIME ZONE
tz``), giving the local wall-clock time the bucket is taken from.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import (
    ColumnElement,
    Date,
    DateTime,
    case,
    cast,
    func,
    literal,
    literal_column,
    null,
    or_,
    select,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.permissions import ScopeFilter
from app.models.enums import (
    TransactionSource,
    TransactionStatus,
    TransactionType,
    UpiAttemptStatus,
)
from app.models.family import Family, FamilyMember
from app.models.finance import Account, Payee, Transaction, TransactionCategory
from app.models.planning import SavingsContribution, SavingsGoal
from app.models.upi import UpiPaymentAttempt
from app.models.user import User
from app.repositories.savings import signed_amount
from app.services.ledger import (
    counted,
    expense_amount,
    income_amount,
    is_spending,
    source_account_delta,
)

CENT = Decimal("0.01")
Unit = Literal["day", "week", "month", "year"]
_UNITS: frozenset[str] = frozenset({"day", "week", "month", "year"})
OPEN_UPI_STATUSES = (
    UpiAttemptStatus.INITIATED,
    UpiAttemptStatus.LAUNCHED,
    UpiAttemptStatus.PENDING_REPORTED,
)


def money(value: Any) -> Decimal:
    return Decimal(value or 0).quantize(CENT)


def _unit_sql(unit: str) -> ColumnElement[Any]:
    # Whitelisted literal: date_trunc's unit and the interval must be SQL literals so the
    # expression in SELECT and GROUP BY is identical.
    if unit not in _UNITS:  # pragma: no cover - guarded by the schema Literal
        raise ValueError(f"unsupported unit {unit!r}")
    return literal_column(f"'{unit}'")


def local_bucket(tz_name: str, column: Any, unit: str) -> ColumnElement[Any]:
    """``date_trunc(unit, column AT TIME ZONE tz)`` (a local timestamp without zone)."""
    return func.date_trunc(_unit_sql(unit), func.timezone(tz_name, column))


def _naive(day: date) -> ColumnElement[Any]:
    return cast(literal(datetime.combine(day, time.min), DateTime()), DateTime())


@dataclass(frozen=True)
class CategoryRow:
    category_id: uuid.UUID | None
    name: str | None
    icon: str | None
    color: str | None
    amount: Decimal
    count: int
    has_children: bool


@dataclass(frozen=True)
class MemberRow:
    user_id: uuid.UUID
    full_name: str
    expense: Decimal
    income: Decimal
    count: int


@dataclass(frozen=True)
class RecentRow:
    tx: Transaction
    category_id: uuid.UUID | None
    category_name: str | None
    category_icon: str | None
    category_color: str | None
    account_name: str | None
    payee_name: str | None
    created_by_name: str


class ReportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ scope helpers

    async def currencies(self, scope: ScopeFilter) -> list[str]:
        """Currencies used by live transactions or live accounts in ``scope``, most used
        first."""
        t, a = Transaction, Account
        tx = select(t.currency.label("currency")).where(scope.clause(t), t.deleted_at.is_(None))
        acc = select(a.currency.label("currency")).where(scope.clause(a), a.deleted_at.is_(None))
        u = union_all(tx, acc).subquery()
        rows = await self.session.execute(
            select(u.c.currency, func.count())
            .group_by(u.c.currency)
            .order_by(func.count().desc(), u.c.currency)
        )
        return [c for c, _ in rows]

    async def family_currency(self, family_id: uuid.UUID) -> str | None:
        return await self.session.scalar(select(Family.currency).where(Family.id == family_id))

    # ------------------------------------------------------------------ transactions

    async def bucketed(
        self,
        measures: Mapping[str, ColumnElement[Any]],
        where: Sequence[ColumnElement[bool]],
        *,
        tz_name: str,
        unit: Unit,
        first_bucket: date,
        last_bucket: date,
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[tuple[date, dict[str, Decimal]]]:
        """``SUM(measure)`` of counted rows per local ``unit`` bucket, zero-filled from
        ``first_bucket`` to ``last_bucket`` (both already truncated to ``unit``)."""
        t = Transaction
        bucket = local_bucket(tz_name, t.occurred_at, unit)
        agg = (
            select(
                bucket.label("bucket"),
                *[func.sum(expr).label(name) for name, expr in measures.items()],
            )
            .where(counted(), t.occurred_at >= start_utc, t.occurred_at < end_utc, *where)
            .group_by(bucket)
            .subquery("agg")
        )
        series = select(
            func.generate_series(
                _naive(first_bucket),
                _naive(last_bucket),
                literal_column(f"interval '1 {unit}'"),
            ).label("bucket")
        ).subquery("series")
        stmt = (
            select(series.c.bucket, *[func.coalesce(agg.c[name], 0) for name in measures])
            .select_from(series)
            .outerjoin(agg, agg.c.bucket == series.c.bucket)
            .order_by(series.c.bucket)
        )
        out: list[tuple[date, dict[str, Decimal]]] = []
        for row in await self.session.execute(stmt):
            values = {name: money(row[i + 1]) for i, name in enumerate(measures)}
            out.append((row[0].date(), values))
        return out

    async def category_totals(
        self,
        where: Sequence[ColumnElement[bool]],
        *,
        kind: Literal["expense", "income"] = "expense",
        parent_id: uuid.UUID | None = None,
    ) -> list[CategoryRow]:
        """Counted spending (or income) per top-level category (subcategories rolled up into
        their parent), or — with ``parent_id`` — per direct category under that parent (the
        parent's own rows included). Ordered by amount, largest first."""
        t = Transaction
        c = aliased(TransactionCategory, name="c")
        if kind == "expense":
            amount, type_ok = expense_amount(), is_spending()
        else:
            amount, type_ok = income_amount(), t.type == TransactionType.INCOME
        total = func.sum(amount)
        if parent_id is None:
            top = aliased(TransactionCategory, name="top")
            stmt = (
                select(
                    top.id,
                    top.name,
                    top.icon,
                    top.color,
                    total,
                    func.count(),
                    func.coalesce(func.bool_or(c.parent_id.is_not(None)), False),
                )
                .select_from(t)
                .outerjoin(c, c.id == t.category_id)
                .outerjoin(top, top.id == func.coalesce(c.parent_id, c.id))
                .where(counted(), type_ok, *where)
                .group_by(top.id, top.name, top.icon, top.color)
            )
        else:
            stmt = (
                select(c.id, c.name, c.icon, c.color, total, func.count(), literal(False))
                .select_from(t)
                .join(c, c.id == t.category_id)
                .where(
                    counted(),
                    type_ok,
                    or_(c.id == parent_id, c.parent_id == parent_id),
                    *where,
                )
                .group_by(c.id, c.name, c.icon, c.color)
            )
        rows = [
            CategoryRow(r[0], r[1], r[2], r[3], money(r[4]), int(r[5]), bool(r[6]))
            for r in await self.session.execute(stmt)
        ]
        rows.sort(key=lambda r: (-r.amount, (r.name or "").lower()))
        return rows

    async def member_totals(self, where: Sequence[ColumnElement[bool]]) -> list[MemberRow]:
        """Counted spending / income per recording user (``owner_user_id``)."""
        t = Transaction
        rows = await self.session.execute(
            select(
                t.owner_user_id,
                User.full_name,
                func.sum(expense_amount()),
                func.sum(income_amount()),
                func.count().filter(is_spending()),
            )
            .join(User, User.id == t.owner_user_id)
            .where(counted(), *where)
            .group_by(t.owner_user_id, User.full_name)
        )
        return [MemberRow(r[0], r[1], money(r[2]), money(r[3]), int(r[4])) for r in rows]

    async def active_members(self, family_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
        rows = await self.session.execute(
            select(FamilyMember.user_id, User.full_name)
            .join(User, User.id == FamilyMember.user_id)
            .where(FamilyMember.family_id == family_id, FamilyMember.left_at.is_(None))
            .order_by(FamilyMember.joined_at, FamilyMember.user_id)
        )
        return [(uid, name) for uid, name in rows]

    async def family_spending(
        self, where: Sequence[ColumnElement[bool]]
    ) -> list[tuple[uuid.UUID, str, Decimal]]:
        """Counted spending per family (``family_id`` NOT NULL rows only)."""
        t = Transaction
        rows = await self.session.execute(
            select(t.family_id, Family.name, func.sum(expense_amount()))
            .join(Family, Family.id == t.family_id)
            .where(counted(), is_spending(), *where)
            .group_by(t.family_id, Family.name)
            .order_by(func.sum(expense_amount()).desc(), Family.name)
        )
        return [(fid, name, money(total)) for fid, name, total in rows]

    async def spending_split(
        self, personal: ColumnElement[bool], family: ColumnElement[bool]
    ) -> tuple[Decimal, Decimal]:
        """Counted spending matching ``personal`` and matching ``family`` in one scan.
        (``personal``/``family`` must also carry the currency/date conditions.)"""
        row = (
            await self.session.execute(
                select(
                    func.sum(case((personal, expense_amount()), else_=0)),
                    func.sum(case((family, expense_amount()), else_=0)),
                ).where(counted(), is_spending(), or_(personal, family))
            )
        ).one()
        return money(row[0]), money(row[1])

    async def payment_methods(
        self, where: Sequence[ColumnElement[bool]]
    ) -> list[tuple[str, Decimal, int]]:
        t = Transaction
        rows = await self.session.execute(
            select(t.payment_method, func.sum(expense_amount()), func.count())
            .where(counted(), is_spending(), *where)
            .group_by(t.payment_method)
        )
        out = [
            (m.value if m is not None else "unspecified", money(total), int(n))
            for m, total, n in rows
        ]
        out.sort(key=lambda r: (-r[1], r[0]))
        return out

    async def recent(self, where: Sequence[ColumnElement[bool]], limit: int) -> list[RecentRow]:
        """Latest live completed/pending rows with category, account, payee and creator."""
        t = Transaction
        c = aliased(TransactionCategory, name="c")
        rows = await self.session.execute(
            select(t, c.id, c.name, c.icon, c.color, Account.name, Payee.name, User.full_name)
            .outerjoin(c, c.id == t.category_id)
            .outerjoin(Account, Account.id == t.account_id)
            .outerjoin(Payee, Payee.id == t.payee_id)
            .join(User, User.id == t.owner_user_id)
            .where(
                t.deleted_at.is_(None),
                t.status.in_([TransactionStatus.COMPLETED, TransactionStatus.PENDING]),
                *where,
            )
            .order_by(t.occurred_at.desc(), t.created_at.desc(), t.id)
            .limit(limit)
        )
        return [RecentRow(*r) for r in rows]

    async def count_pending_upi(self, where: Sequence[ColumnElement[bool]]) -> int:
        t = Transaction
        n = await self.session.scalar(
            select(func.count()).where(
                t.deleted_at.is_(None),
                t.status == TransactionStatus.PENDING,
                t.source == TransactionSource.UPI_INTENT,
                *where,
            )
        )
        return int(n or 0)

    # ------------------------------------------------------------------ accounts

    async def scope_accounts(self, scope: ScopeFilter, currency: str) -> list[Account]:
        """Live, active accounts visible through ``scope`` in ``currency``."""
        stmt = scope.apply(select(Account), Account).where(
            Account.is_active.is_(True), Account.currency == currency
        )
        return list(await self.session.scalars(stmt.order_by(Account.name, Account.id)))

    async def visible_account(self, user_id: uuid.UUID, account_id: uuid.UUID) -> Account | None:
        stmt = ScopeFilter.for_user(user_id).apply(
            select(Account).where(Account.id == account_id), Account
        )
        return await self.session.scalar(stmt)

    async def account_daily_changes(
        self, account_id: uuid.UUID, tz_name: str, since_utc: datetime
    ) -> dict[date, Decimal]:
        """Net counted movement on ``account_id`` per local day, for rows at/after
        ``since_utc`` (future-dated rows included)."""
        t = Transaction
        outgoing = select(t.occurred_at.label("at"), source_account_delta().label("delta")).where(
            counted(), t.account_id == account_id, t.occurred_at >= since_utc
        )
        incoming = select(t.occurred_at.label("at"), t.amount.label("delta")).where(
            counted(),
            t.type == TransactionType.TRANSFER,
            t.to_account_id == account_id,
            t.occurred_at >= since_utc,
        )
        u = union_all(outgoing, incoming).subquery()
        day = cast(func.timezone(tz_name, u.c.at), Date)
        rows = await self.session.execute(select(day, func.sum(u.c.delta)).group_by(day))
        return {d: money(v) for d, v in rows}

    # ------------------------------------------------------------------ savings

    async def scope_goals(self, scope: ScopeFilter, currency: str) -> list[SavingsGoal]:
        stmt = scope.apply(select(SavingsGoal), SavingsGoal).where(SavingsGoal.currency == currency)
        return list(await self.session.scalars(stmt.order_by(SavingsGoal.created_at)))

    async def savings_by_month(
        self,
        goal_ids: Sequence[uuid.UUID],
        tz_name: str,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[Decimal, dict[date, tuple[Decimal, Decimal]]]:
        """``(balance before start, {local month: (deposits, withdrawals)})`` over live
        contributions of ``goal_ids`` up to ``end_utc``."""
        if not goal_ids:
            return Decimal("0.00"), {}
        c = SavingsContribution
        bucket = case(
            (c.contributed_at < start_utc, null()),
            else_=local_bucket(tz_name, c.contributed_at, "month"),
        )
        signed = signed_amount()
        rows = await self.session.execute(
            select(
                bucket,
                func.sum(case((signed > 0, signed), else_=0)),
                func.sum(case((signed < 0, -signed), else_=0)),
            )
            .where(
                c.goal_id.in_(list(goal_ids)),
                c.deleted_at.is_(None),
                c.contributed_at < end_utc,
            )
            .group_by(bucket)
        )
        opening = Decimal("0.00")
        months: dict[date, tuple[Decimal, Decimal]] = {}
        for b, dep, wd in rows:
            if b is None:
                opening = money(dep) - money(wd)
            else:
                months[b.date()] = (money(dep), money(wd))
        return opening, months

    # ------------------------------------------------------------------ categories

    async def categories(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, TransactionCategory]:
        if not ids:
            return {}
        rows = await self.session.scalars(
            select(TransactionCategory).where(TransactionCategory.id.in_(list(set(ids))))
        )
        return {c.id: c for c in rows}

    async def category(self, category_id: uuid.UUID) -> TransactionCategory | None:
        return await self.session.get(TransactionCategory, category_id)

    # ------------------------------------------------------------------ UPI

    @staticmethod
    def upi_attempts_where(
        scope: ScopeFilter, currency: str, start_utc: datetime, end_utc: datetime
    ) -> list[ColumnElement[bool]]:
        a = UpiPaymentAttempt
        return [
            scope.clause(a),
            a.currency == currency,
            a.initiated_at >= start_utc,
            a.initiated_at < end_utc,
        ]

    async def upi_by_status(
        self, where: Sequence[ColumnElement[bool]]
    ) -> list[tuple[UpiAttemptStatus, int, Decimal]]:
        a = UpiPaymentAttempt
        rows = await self.session.execute(
            select(a.status, func.count(), func.sum(a.amount)).where(*where).group_by(a.status)
        )
        return [(s, int(n), money(total)) for s, n, total in rows]

    async def upi_top_payees(
        self, where: Sequence[ColumnElement[bool]], limit: int = 10
    ) -> list[tuple[str, str | None, int, Decimal]]:
        a = UpiPaymentAttempt
        total = func.sum(a.amount)
        rows = await self.session.execute(
            select(a.payee_vpa, func.max(a.payee_name), func.count(), total)
            .where(a.status == UpiAttemptStatus.SUCCESS_REPORTED, *where)
            .group_by(a.payee_vpa)
            .order_by(total.desc(), func.count().desc(), a.payee_vpa)
            .limit(limit)
        )
        return [(vpa, name, int(n), money(s)) for vpa, name, n, s in rows]

    @staticmethod
    def linked_to_attempts(where: Sequence[ColumnElement[bool]]) -> ColumnElement[bool]:
        """Transactions linked (either direction) to an attempt matching ``where``."""
        a = UpiPaymentAttempt
        t = Transaction
        return or_(
            t.upi_attempt_id.in_(select(a.id).where(*where)),
            t.id.in_(select(a.transaction_id).where(a.transaction_id.is_not(None), *where)),
        )

    async def spending_total(self, where: Sequence[ColumnElement[bool]]) -> Decimal:
        total = await self.session.scalar(
            select(func.sum(expense_amount())).where(counted(), is_spending(), *where)
        )
        return money(total)
