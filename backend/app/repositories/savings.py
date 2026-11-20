"""Queries for savings goals and their contributions."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import ColumnElement, Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import ScopeFilter
from app.models.enums import ContributionKind, GoalStatus, RecordScope
from app.models.finance import Transaction
from app.models.planning import SavingsContribution, SavingsGoal
from app.models.user import User

CENT = Decimal("0.01")


def signed_amount() -> ColumnElement[Decimal]:
    c = SavingsContribution
    return case((c.kind == ContributionKind.WITHDRAWAL, -c.amount), else_=c.amount)


class SavingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, obj: SavingsGoal | SavingsContribution) -> None:
        self.session.add(obj)

    async def get_visible(
        self, user_id: uuid.UUID, goal_id: uuid.UUID, *, for_update: bool = False
    ) -> SavingsGoal | None:
        stmt = ScopeFilter.for_user(user_id).apply(
            select(SavingsGoal).where(SavingsGoal.id == goal_id), SavingsGoal
        )
        if for_update:
            stmt = stmt.with_for_update(of=SavingsGoal)
        return await self.session.scalar(stmt)

    async def list_for_scope(
        self, user_id: uuid.UUID, family_id: uuid.UUID | None, status: GoalStatus | None
    ) -> list[SavingsGoal]:
        scope = (
            ScopeFilter.for_user(user_id, family_id=family_id)
            if family_id is not None
            else ScopeFilter.for_user(user_id, scope=RecordScope.PERSONAL)
        )
        stmt = scope.apply(select(SavingsGoal), SavingsGoal)
        if status is not None:
            stmt = stmt.where(SavingsGoal.status == status)
        return list(
            await self.session.scalars(stmt.order_by(SavingsGoal.created_at, SavingsGoal.id))
        )

    async def balances(self, goal_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Decimal]:
        """``deposits - withdrawals`` of live contributions per goal (0 when none)."""
        out = {gid: Decimal("0.00") for gid in goal_ids}
        if not goal_ids:
            return out
        c = SavingsContribution
        rows = await self.session.execute(
            select(c.goal_id, func.coalesce(func.sum(signed_amount()), 0))
            .where(c.goal_id.in_(list(goal_ids)), c.deleted_at.is_(None))
            .group_by(c.goal_id)
        )
        for gid, total in rows:
            out[gid] = Decimal(total).quantize(CENT)
        return out

    async def get_contribution(
        self, goal_id: uuid.UUID, contribution_id: uuid.UUID
    ) -> SavingsContribution | None:
        c = SavingsContribution
        return await self.session.scalar(
            select(c).where(c.id == contribution_id, c.goal_id == goal_id, c.deleted_at.is_(None))
        )

    def contributions_stmt(self, goal_id: uuid.UUID) -> Select[SavingsContribution]:
        c = SavingsContribution
        return (
            select(c)
            .where(c.goal_id == goal_id, c.deleted_at.is_(None))
            .order_by(c.contributed_at.desc(), c.created_at.desc(), c.id)
        )

    async def monthly_totals(
        self, goal_id: uuid.UUID, tz_name: str
    ) -> list[tuple[str, Decimal, Decimal]]:
        """``(YYYY-MM, deposits, withdrawals)`` per month (in ``tz_name``) with activity."""
        c = SavingsContribution
        month = func.to_char(func.timezone(tz_name, c.contributed_at), "YYYY-MM")
        rows = await self.session.execute(
            select(
                month,
                func.coalesce(
                    func.sum(case((c.kind == ContributionKind.DEPOSIT, c.amount), else_=0)), 0
                ),
                func.coalesce(
                    func.sum(case((c.kind == ContributionKind.WITHDRAWAL, c.amount), else_=0)), 0
                ),
            )
            .where(c.goal_id == goal_id, c.deleted_at.is_(None))
            .group_by(month)
            .order_by(month)
        )
        return [(m, Decimal(d).quantize(CENT), Decimal(w).quantize(CENT)) for m, d, w in rows]

    async def visible_transaction(
        self, user_id: uuid.UUID, transaction_id: uuid.UUID
    ) -> Transaction | None:
        """A live transaction ``user_id`` can see (any scope)."""
        stmt = ScopeFilter.for_user(user_id).apply(
            select(Transaction).where(Transaction.id == transaction_id), Transaction
        )
        return await self.session.scalar(stmt)

    async def user_names(self, user_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        ids = list(set(user_ids))
        if not ids:
            return {}
        rows = await self.session.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
        return {uid: name for uid, name in rows}

    async def user_timezones(self, user_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        ids = list(set(user_ids))
        if not ids:
            return {}
        rows = await self.session.execute(select(User.id, User.timezone).where(User.id.in_(ids)))
        return {uid: tz for uid, tz in rows}
