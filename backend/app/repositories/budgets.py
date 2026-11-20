"""Queries for budgets, their category limits and budget spending.

Spending always goes through :mod:`app.services.ledger` (``counted()`` / ``expense_amount()``)
so budgets agree with reports and the dashboard. Visibility of budgets themselves is applied
with :class:`app.core.permissions.ScopeFilter`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.permissions import ScopeFilter
from app.models.enums import CategoryKind, FamilyRole, RecordScope
from app.models.family import Family, FamilyMember
from app.models.finance import Transaction, TransactionCategory
from app.models.planning import Budget, BudgetCategory
from app.models.user import User
from app.services import ledger

CENT = Decimal("0.01")


@dataclass(frozen=True)
class SpendScope:
    """Which transactions a budget looks at (independent of the period). ``currency``: only
    transactions in that currency count (a budget never adds up different currencies)."""

    scope: RecordScope
    owner_user_id: uuid.UUID
    family_id: uuid.UUID | None
    member_user_id: uuid.UUID | None = None
    currency: str | None = None

    @classmethod
    def of(cls, budget: Budget) -> SpendScope:
        return cls(
            budget.scope,
            budget.owner_user_id,
            budget.family_id,
            budget.member_user_id,
            budget.currency,
        )

    def clauses(self) -> list[ColumnElement[bool]]:
        t = Transaction
        if self.scope is RecordScope.PERSONAL:
            out = [t.scope == RecordScope.PERSONAL, t.owner_user_id == self.owner_user_id]
        else:
            out = [t.scope == RecordScope.FAMILY, t.family_id == self.family_id]
        if self.member_user_id is not None:
            out.append(t.owner_user_id == self.member_user_id)
        if self.currency is not None:
            out.append(t.currency == self.currency)
        return out


def category_tree(root_ids: Iterable[uuid.UUID]) -> Any:
    """Recursive CTE ``(root, id)``: every category id under each root (root included).

    ``UNION`` (not ``UNION ALL``) so an accidental parent cycle still terminates. Soft-deleted
    subcategories are included: spending recorded against them is still spending."""
    c = TransactionCategory
    base = select(c.id.label("root"), c.id.label("id")).where(c.id.in_(list(root_ids)))
    tree = base.cte("budget_category_tree", recursive=True)
    child = select(tree.c.root, c.id).where(c.parent_id == tree.c.id)
    return tree.union(child)


def _quantize(value: Any) -> Decimal:
    return Decimal(value or 0).quantize(CENT)


class BudgetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, obj: Budget | BudgetCategory) -> None:
        self.session.add(obj)

    # --- budgets ---------------------------------------------------------------------

    async def get_visible(
        self, user_id: uuid.UUID, budget_id: uuid.UUID, *, for_update: bool = False
    ) -> Budget | None:
        stmt = ScopeFilter.for_user(user_id).apply(
            select(Budget).where(Budget.id == budget_id), Budget
        )
        if for_update:
            stmt = stmt.with_for_update(of=Budget)
        return await self.session.scalar(stmt)

    async def list_for_scope(
        self,
        user_id: uuid.UUID,
        family_id: uuid.UUID | None,
        *,
        is_active: bool | None = None,
        period: Any = None,
    ) -> list[Budget]:
        scope = (
            ScopeFilter.for_user(user_id, family_id=family_id)
            if family_id is not None
            else ScopeFilter.for_user(user_id, scope=RecordScope.PERSONAL)
        )
        stmt = scope.apply(select(Budget), Budget)
        if is_active is not None:
            stmt = stmt.where(Budget.is_active.is_(is_active))
        if period is not None:
            stmt = stmt.where(Budget.period == period)
        stmt = stmt.order_by(Budget.created_at, Budget.id)
        return list(await self.session.scalars(stmt))

    async def list_active_for_alerts(self) -> list[tuple[Budget, str]]:
        """Every live, active budget (of a live family, if family-scoped) with its owner's
        timezone."""
        rows = await self.session.execute(
            select(Budget, User.timezone)
            .join(User, User.id == Budget.owner_user_id)
            .outerjoin(Family, Family.id == Budget.family_id)
            .where(
                Budget.deleted_at.is_(None),
                Budget.is_active.is_(True),
                (Budget.family_id.is_(None)) | (Family.deleted_at.is_(None)),
            )
            .order_by(Budget.created_at, Budget.id)
        )
        return [(b, tz) for b, tz in rows]

    async def owner_timezone(self, user_id: uuid.UUID) -> str:
        tz = await self.session.scalar(select(User.timezone).where(User.id == user_id))
        return tz or "UTC"

    async def owner_timezones(self, user_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
        ids = list(set(user_ids))
        if not ids:
            return {}
        rows = await self.session.execute(select(User.id, User.timezone).where(User.id.in_(ids)))
        return {uid: tz for uid, tz in rows}

    async def is_active_member(self, family_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        found = await self.session.scalar(
            select(FamilyMember.id).where(
                FamilyMember.family_id == family_id,
                FamilyMember.user_id == user_id,
                FamilyMember.left_at.is_(None),
            )
        )
        return found is not None

    async def alert_recipients(self, budget: Budget) -> list[uuid.UUID]:
        """personal → owner; family → active owner/admins (+ the member of a member budget)."""
        if budget.scope is RecordScope.PERSONAL:
            return [budget.owner_user_id]
        if budget.family_id is None:  # pragma: no cover - enforced by ck_*_scope_family
            raise NotFoundError()
        rows = await self.session.execute(
            select(FamilyMember.user_id, FamilyMember.role).where(
                FamilyMember.family_id == budget.family_id, FamilyMember.left_at.is_(None)
            )
        )
        out: list[uuid.UUID] = []
        for uid, role in rows:
            if role in (FamilyRole.OWNER, FamilyRole.ADMIN) or uid == budget.member_user_id:
                out.append(uid)
        return sorted(out)

    # --- category limits -------------------------------------------------------------

    async def list_categories(
        self, budget_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[tuple[BudgetCategory, str]]]:
        """Each budget's category limits with the category name. Limits pointing at a
        soft-deleted category are *stale* and left out (so they never count towards spending or
        appear in responses); category deletion normally moves or removes them anyway."""
        out: dict[uuid.UUID, list[tuple[BudgetCategory, str]]] = {bid: [] for bid in budget_ids}
        if not budget_ids:
            return out
        rows = await self.session.execute(
            select(BudgetCategory, TransactionCategory.name)
            .join(TransactionCategory, TransactionCategory.id == BudgetCategory.category_id)
            .where(
                BudgetCategory.budget_id.in_(list(budget_ids)),
                TransactionCategory.deleted_at.is_(None),
            )
            .order_by(TransactionCategory.sort_order, TransactionCategory.name, BudgetCategory.id)
        )
        for bc, name in rows:
            out[bc.budget_id].append((bc, name))
        return out

    async def replace_categories(
        self, budget_id: uuid.UUID, items: Sequence[tuple[uuid.UUID, Decimal]]
    ) -> None:
        await self.session.execute(
            delete(BudgetCategory).where(BudgetCategory.budget_id == budget_id)
        )
        for category_id, amount in items:
            self.session.add(
                BudgetCategory(budget_id=budget_id, category_id=category_id, amount=amount)
            )

    async def usable_expense_categories(
        self,
        category_ids: Sequence[uuid.UUID],
        *,
        owner_user_id: uuid.UUID,
        family_id: uuid.UUID | None,
    ) -> set[uuid.UUID]:
        """Subset of ``category_ids`` that are live *expense* categories usable in the budget's
        scope: built-ins + the owner's personal ones (personal budget) or built-ins + that
        family's ones (family budget)."""
        if not category_ids:
            return set()
        c = TransactionCategory
        builtin = and_(c.owner_user_id.is_(None), c.family_id.is_(None))
        own = (
            c.family_id == family_id if family_id is not None else c.owner_user_id == owner_user_id
        )
        rows = await self.session.scalars(
            select(c.id).where(
                c.id.in_(list(category_ids)),
                c.deleted_at.is_(None),
                c.kind == CategoryKind.EXPENSE,
                builtin | own,
            )
        )
        return set(rows)

    # --- spending --------------------------------------------------------------------

    async def spent(
        self,
        scope: SpendScope,
        start: datetime,
        end: datetime,
        category_ids: Sequence[uuid.UUID] | None = None,
    ) -> Decimal:
        """Net spending (expenses - refunds) of counted rows in ``[start, end)``. With
        ``category_ids`` only rows in those categories or their subcategories count."""
        t = Transaction
        stmt = select(func.coalesce(func.sum(ledger.expense_amount()), 0)).where(
            ledger.counted(),
            ledger.is_spending(),
            t.occurred_at >= start,
            t.occurred_at < end,
            *scope.clauses(),
        )
        if category_ids is not None:
            if not category_ids:
                return Decimal("0.00")
            tree = category_tree(category_ids)
            stmt = stmt.where(t.category_id.in_(select(tree.c.id)))
        return _quantize(await self.session.scalar(stmt))

    async def spent_by_category(
        self,
        scope: SpendScope,
        start: datetime,
        end: datetime,
        category_ids: Sequence[uuid.UUID],
    ) -> dict[uuid.UUID, Decimal]:
        """``{root_category_id: net spending}`` — each root includes its subcategories."""
        out = {cid: Decimal("0.00") for cid in category_ids}
        if not category_ids:
            return out
        t = Transaction
        tree = category_tree(category_ids)
        rows = await self.session.execute(
            select(tree.c.root, func.coalesce(func.sum(ledger.expense_amount()), 0))
            .select_from(t)
            .join(tree, tree.c.id == t.category_id)
            .where(
                ledger.counted(),
                ledger.is_spending(),
                t.occurred_at >= start,
                t.occurred_at < end,
                *scope.clauses(),
            )
            .group_by(tree.c.root)
        )
        for root, total in rows:
            out[root] = _quantize(total)
        return out
