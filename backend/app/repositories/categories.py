"""Queries for categories and tags.

A category *context* is where a category may be used:

* personal context (``family_id is None``): the built-ins + the user's own personal categories;
* family context: the built-ins + that family's categories (only while the user is an active
  member — the membership test is part of the SQL, see ``app.core.permissions``).

Soft-deleted rows are excluded everywhere except the bulk updates used when deleting.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ColumnElement, and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import ScopeFilter, visible_categories_clause
from app.models.enums import CategoryKind
from app.models.finance import Payee, Transaction, TransactionCategory, TransactionTag
from app.models.planning import BudgetCategory, RecurringTransaction

C = TransactionCategory


def builtin_clause() -> ColumnElement[bool]:
    return and_(C.owner_user_id.is_(None), C.family_id.is_(None))


def context_clause(user_id: uuid.UUID, family_id: uuid.UUID | None) -> ColumnElement[bool]:
    """Categories usable in the personal context (``family_id=None``) or in ``family_id``."""
    if family_id is None:
        return or_(builtin_clause(), C.owner_user_id == user_id)
    return visible_categories_clause(user_id, family_id=family_id)


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class CategoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, category: TransactionCategory) -> None:
        self.session.add(category)

    async def get_live(
        self, category_id: uuid.UUID, *, for_update: bool = False
    ) -> TransactionCategory | None:
        stmt = select(C).where(C.id == category_id, C.deleted_at.is_(None))
        if for_update:
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt)

    async def get_visible(
        self, user_id: uuid.UUID, category_id: uuid.UUID
    ) -> TransactionCategory | None:
        """A live category the user can see in any context (built-in, own, any family's)."""
        return await self.session.scalar(
            select(C).where(
                C.id == category_id, C.deleted_at.is_(None), visible_categories_clause(user_id)
            )
        )

    async def get_in_context(
        self,
        user_id: uuid.UUID,
        family_id: uuid.UUID | None,
        category_id: uuid.UUID,
        *,
        active_only: bool = True,
    ) -> TransactionCategory | None:
        """A live category usable in the context (``active_only``: not archived either)."""
        stmt = select(C).where(
            C.id == category_id, C.deleted_at.is_(None), context_clause(user_id, family_id)
        )
        if active_only:
            stmt = stmt.where(C.is_archived.is_(False))
        return await self.session.scalar(stmt)

    async def list_in_context(
        self,
        user_id: uuid.UUID,
        family_id: uuid.UUID | None,
        *,
        kind: CategoryKind | None = None,
        include_archived: bool = False,
        parent_id: uuid.UUID | None = None,
    ) -> list[TransactionCategory]:
        stmt = select(C).where(C.deleted_at.is_(None), context_clause(user_id, family_id))
        if kind is not None:
            stmt = stmt.where(C.kind == kind)
        if not include_archived:
            stmt = stmt.where(C.is_archived.is_(False))
        if parent_id is not None:
            stmt = stmt.where(C.parent_id == parent_id)
        stmt = stmt.order_by(C.sort_order, func.lower(C.name), C.id)
        return list(await self.session.scalars(stmt))

    async def sibling_name_taken(
        self,
        user_id: uuid.UUID,
        family_id: uuid.UUID | None,
        *,
        parent_id: uuid.UUID | None,
        kind: CategoryKind,
        name: str,
        exclude_id: uuid.UUID | None = None,
    ) -> bool:
        """Is ``name`` (case-insensitive) already used by a live sibling in the context —
        including a built-in with the same parent and kind?"""
        stmt = select(C.id).where(
            C.deleted_at.is_(None),
            context_clause(user_id, family_id),
            C.parent_id.is_(None) if parent_id is None else C.parent_id == parent_id,
            C.kind == kind,
            func.lower(C.name) == name.lower(),
        )
        if exclude_id is not None:
            stmt = stmt.where(C.id != exclude_id)
        return (await self.session.scalar(stmt.limit(1))) is not None

    async def live_child_ids(self, parent_id: uuid.UUID) -> list[uuid.UUID]:
        return list(
            await self.session.scalars(
                select(C.id).where(C.parent_id == parent_id, C.deleted_at.is_(None))
            )
        )

    # --- deletion -----------------------------------------------------------------------

    async def count_live_transactions(self, category_ids: Sequence[uuid.UUID]) -> int:
        count = await self.session.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.category_id.in_(category_ids), Transaction.deleted_at.is_(None))
        )
        return int(count or 0)

    async def count_live_recurring(self, category_ids: Sequence[uuid.UUID]) -> int:
        count = await self.session.scalar(
            select(func.count())
            .select_from(RecurringTransaction)
            .where(
                RecurringTransaction.category_id.in_(category_ids),
                RecurringTransaction.deleted_at.is_(None),
            )
        )
        return int(count or 0)

    async def reassign_transactions(
        self, category_ids: Sequence[uuid.UUID], target_id: uuid.UUID
    ) -> int:
        """Point every transaction (soft-deleted ones too, so a restore stays consistent) at
        ``target_id``. Returns how many live transactions moved."""
        live = await self.count_live_transactions(category_ids)
        await self.session.execute(
            update(Transaction)
            .where(Transaction.category_id.in_(category_ids))
            .values(category_id=target_id)
            .execution_options(synchronize_session=False)
        )
        return live

    async def reassign_recurring(
        self, category_ids: Sequence[uuid.UUID], target_id: uuid.UUID
    ) -> int:
        live = await self.count_live_recurring(category_ids)
        await self.session.execute(
            update(RecurringTransaction)
            .where(RecurringTransaction.category_id.in_(category_ids))
            .values(category_id=target_id)
            .execution_options(synchronize_session=False)
        )
        return live

    async def reassign_payee_defaults(
        self, category_ids: Sequence[uuid.UUID], target_id: uuid.UUID | None
    ) -> None:
        await self.session.execute(
            update(Payee)
            .where(Payee.default_category_id.in_(category_ids))
            .values(default_category_id=target_id)
            .execution_options(synchronize_session=False)
        )

    async def move_budget_limits(
        self, category_ids: Sequence[uuid.UUID], target_id: uuid.UUID
    ) -> int:
        """Move every budget category limit on ``category_ids`` to ``target_id``.

        Per budget, the limits being moved are summed; if the budget already has a limit on
        ``target_id`` the sum is added to it, otherwise one of the moved rows is repointed to
        ``target_id`` with the summed amount (``(budget_id, category_id)`` is unique). Rows are
        locked first so a concurrent budget edit can't interleave. Returns the rows moved."""
        rows = list(
            await self.session.scalars(
                select(BudgetCategory)
                .where(BudgetCategory.category_id.in_([*category_ids, target_id]))
                .order_by(BudgetCategory.budget_id, BudgetCategory.created_at, BudgetCategory.id)
                .with_for_update()
            )
        )
        by_budget: defaultdict[uuid.UUID, list[BudgetCategory]] = defaultdict(list)
        for row in rows:
            by_budget[row.budget_id].append(row)
        moved = 0
        for budget_rows in by_budget.values():
            sources = [r for r in budget_rows if r.category_id != target_id]
            if not sources:
                continue
            moved += len(sources)
            total = sum((r.amount for r in sources), Decimal("0"))
            target = next((r for r in budget_rows if r.category_id == target_id), None)
            if target is None:
                target, *sources = sources
                target.category_id = target_id
                target.amount = total
            else:
                target.amount = target.amount + total
            for row in sources:
                await self.session.delete(row)
        await self.session.flush()
        return moved

    async def remove_budget_limits(self, category_ids: Sequence[uuid.UUID]) -> int:
        """Drop budget category limits on ``category_ids`` (category deleted, nothing moved)."""
        result = await self.session.execute(
            delete(BudgetCategory)
            .where(BudgetCategory.category_id.in_(category_ids))
            .returning(BudgetCategory.id)
            .execution_options(synchronize_session=False)
        )
        return len(result.all())

    async def soft_delete(self, category_ids: Sequence[uuid.UUID], now: datetime) -> None:
        await self.session.execute(
            update(C)
            .where(C.id.in_(category_ids), C.deleted_at.is_(None))
            .values(deleted_at=now)
            .execution_options(synchronize_session=False)
        )


class TagRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def counts(
        self, scope: ScopeFilter, *, q: str | None = None, limit: int = 20
    ) -> list[tuple[str, int]]:
        """Distinct tag names on live transactions visible through ``scope``, most used first."""
        count = func.count().label("n")
        stmt = (
            select(TransactionTag.name, count)
            .join(Transaction, Transaction.id == TransactionTag.transaction_id)
            .where(scope.clause(Transaction), Transaction.deleted_at.is_(None))
            .group_by(TransactionTag.name)
            .order_by(count.desc(), TransactionTag.name)
            .limit(limit)
        )
        if q:
            stmt = stmt.where(TransactionTag.name.like(f"%{escape_like(q.lower())}%"))
        rows = await self.session.execute(stmt)
        return [(name, int(n)) for name, n in rows]
