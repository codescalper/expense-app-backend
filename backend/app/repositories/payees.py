"""Queries for saved payees and the history used for category suggestions."""

from __future__ import annotations

import uuid

from sqlalchemy import ColumnElement, Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import ScopeFilter
from app.models.enums import RecordScope, TransactionType
from app.models.finance import Payee, Transaction, TransactionCategory
from app.models.upi import UpiPaymentAttempt
from app.repositories.categories import context_clause, escape_like
from app.services.ledger import counted


def context_scope(user_id: uuid.UUID, family_id: uuid.UUID | None) -> ScopeFilter:
    """Personal context → the user's personal rows; family context → that family's rows."""
    if family_id is None:
        return ScopeFilter.for_user(user_id, scope=RecordScope.PERSONAL)
    return ScopeFilter.for_user(user_id, family_id=family_id)


class PayeeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, payee: Payee) -> None:
        self.session.add(payee)

    async def get_visible(
        self, user_id: uuid.UUID, payee_id: uuid.UUID, *, for_update: bool = False
    ) -> Payee | None:
        stmt = ScopeFilter.for_user(user_id).apply(select(Payee).where(Payee.id == payee_id), Payee)
        if for_update:
            stmt = stmt.with_for_update(of=Payee)
        return await self.session.scalar(stmt)

    def search_stmt(
        self, user_id: uuid.UUID, family_id: uuid.UUID | None, q: str | None
    ) -> Select[Payee]:
        stmt = context_scope(user_id, family_id).apply(select(Payee), Payee)
        if q:
            pattern = f"%{escape_like(q)}%"
            stmt = stmt.where(
                or_(
                    Payee.name.ilike(pattern),
                    Payee.upi_id.ilike(pattern),
                    Payee.mobile.ilike(pattern),
                )
            )
        return stmt.order_by(func.lower(Payee.name), Payee.id)

    async def recent(
        self, user_id: uuid.UUID, family_id: uuid.UUID | None, limit: int
    ) -> list[Payee]:
        stmt = (
            context_scope(user_id, family_id)
            .apply(select(Payee), Payee)
            .where(Payee.last_used_at.is_not(None))
            .order_by(Payee.last_used_at.desc(), Payee.id)
            .limit(limit)
        )
        return list(await self.session.scalars(stmt))

    async def frequent(
        self, user_id: uuid.UUID, family_id: uuid.UUID | None, limit: int
    ) -> list[Payee]:
        stmt = (
            context_scope(user_id, family_id)
            .apply(select(Payee), Payee)
            .where(Payee.use_count > 0)
            .order_by(Payee.use_count.desc(), Payee.last_used_at.desc().nulls_last(), Payee.id)
            .limit(limit)
        )
        return list(await self.session.scalars(stmt))

    async def find_by_vpa(
        self,
        owner_user_id: uuid.UUID,
        family_id: uuid.UUID | None,
        vpa: str,
        *,
        exclude_id: uuid.UUID | None = None,
        for_update: bool = False,
    ) -> Payee | None:
        """The live payee with this VPA in a scope: the owner's personal payees, or the family's
        payees (whoever created them). No visibility check — callers have authorised."""
        stmt = select(Payee).where(Payee.upi_id == vpa, Payee.deleted_at.is_(None))
        if family_id is None:
            stmt = stmt.where(
                Payee.scope == RecordScope.PERSONAL, Payee.owner_user_id == owner_user_id
            )
        else:
            stmt = stmt.where(Payee.scope == RecordScope.FAMILY, Payee.family_id == family_id)
        if exclude_id is not None:
            stmt = stmt.where(Payee.id != exclude_id)
        if for_update:
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt.order_by(Payee.created_at, Payee.id).limit(1))

    async def lock_vpa(
        self, family_id: uuid.UUID | None, owner_user_id: uuid.UUID, vpa: str
    ) -> None:
        """Transaction-scoped advisory lock serialising create/touch of one VPA in one scope (the
        unique index can't cover family scope because it includes ``owner_user_id``)."""
        key = f"payee:{family_id or 'personal:' + str(owner_user_id)}:{vpa}"
        await self.session.execute(select(func.pg_advisory_xact_lock(func.hashtext(key))))

    # --- category suggestion ------------------------------------------------------------

    async def history_category_counts(
        self,
        user_id: uuid.UUID,
        family_id: uuid.UUID | None,
        *,
        payee_ids: list[uuid.UUID],
        vpa: str | None,
        merchant: str | None,
    ) -> list[tuple[uuid.UUID, int]]:
        """``(category_id, uses)`` over the caller's visible, counted expense transactions that
        match the payee(s) / VPA / merchant, restricted to categories usable in the target
        context. Most used first (ties: most recently used)."""
        matches: list[ColumnElement[bool]] = []
        if payee_ids:
            matches.append(Transaction.payee_id.in_(payee_ids))
        if vpa:
            matches.append(Transaction.payee_id.in_(select(Payee.id).where(Payee.upi_id == vpa)))
            matches.append(
                Transaction.upi_attempt_id.in_(
                    select(UpiPaymentAttempt.id).where(UpiPaymentAttempt.payee_vpa == vpa)
                )
            )
        if merchant:
            matches.append(func.lower(Transaction.merchant) == merchant.lower())
        if not matches:
            return []
        uses = func.count().label("uses")
        stmt = (
            select(Transaction.category_id, uses)
            .join(TransactionCategory, TransactionCategory.id == Transaction.category_id)
            .where(
                ScopeFilter.for_user(user_id).clause(Transaction),
                counted(),
                Transaction.type == TransactionType.EXPENSE,
                or_(*matches),
                TransactionCategory.deleted_at.is_(None),
                TransactionCategory.is_archived.is_(False),
                context_clause(user_id, family_id),
            )
            .group_by(Transaction.category_id)
            .order_by(uses.desc(), func.max(Transaction.occurred_at).desc())
        )
        rows = await self.session.execute(stmt)
        return [(cid, int(n)) for cid, n in rows if cid is not None]
