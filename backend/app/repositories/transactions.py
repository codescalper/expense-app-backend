"""Queries for transactions, their tags and the rows they reference.

Visibility is applied by the service (``ScopeFilter`` clause passed in as ``where``); the
``*_many`` lookups here are for hydrating rows that are already authorised.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Select, delete, func, insert, literal, or_, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.permissions import ScopeFilter, visible_categories_clause
from app.models.enums import PaymentMethod, RecordScope, TransactionStatus, TransactionType
from app.models.finance import (
    Account,
    Payee,
    Transaction,
    TransactionAttachment,
    TransactionCategory,
    TransactionTag,
)
from app.models.user import User
from app.utils.pagination import PageParams, paginate


def escape_like(value: str) -> str:
    """Escape LIKE wildcards so user input matches literally (used with ``escape='\\'``)."""
    return value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


class TransactionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, tx: Transaction) -> None:
        self.session.add(tx)

    # ------------------------------------------------------------------ single rows

    async def get(self, tx_id: uuid.UUID, *, for_update: bool = False) -> Transaction | None:
        """By id, no visibility filter (callers run ``require_record_write`` next)."""
        stmt = select(Transaction).where(Transaction.id == tx_id)
        if for_update:
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt)

    async def get_visible(self, user_id: uuid.UUID, tx_id: uuid.UUID) -> Transaction | None:
        stmt = ScopeFilter.for_user(user_id).apply(
            select(Transaction).where(Transaction.id == tx_id), Transaction
        )
        return await self.session.scalar(stmt)

    async def get_by_idempotency_key(self, owner_id: uuid.UUID, key: str) -> Transaction | None:
        """Includes soft-deleted rows: the unique constraint covers them too."""
        return await self.session.scalar(
            select(Transaction).where(
                Transaction.owner_user_id == owner_id, Transaction.idempotency_key == key
            )
        )

    # ------------------------------------------------------------------ references

    async def get_usable_category(
        self,
        user_id: uuid.UUID,
        category_id: uuid.UUID,
        *,
        scope: RecordScope,
        family_id: uuid.UUID | None,
        owner_user_id: uuid.UUID,
    ) -> TransactionCategory | None:
        """A live category usable for a transaction of this scope: built-ins, plus the
        transaction owner's personal categories (personal scope) or the family's categories
        (family scope, via :func:`visible_categories_clause`)."""
        c = TransactionCategory
        stmt = select(c).where(c.id == category_id, c.deleted_at.is_(None))
        if scope is RecordScope.FAMILY:
            stmt = stmt.where(visible_categories_clause(user_id, family_id=family_id))
        else:
            stmt = stmt.where(
                visible_categories_clause(user_id),
                c.family_id.is_(None),
                or_(c.owner_user_id.is_(None), c.owner_user_id == owner_user_id),
            )
        return await self.session.scalar(stmt)

    async def category_archived(self, category: TransactionCategory) -> bool:
        """Archived itself, or a subcategory of an archived parent."""
        if category.is_archived:
            return True
        if category.parent_id is None:
            return False
        parent_archived = await self.session.scalar(
            select(TransactionCategory.is_archived).where(
                TransactionCategory.id == category.parent_id
            )
        )
        return bool(parent_archived)

    async def get_visible_payee(self, user_id: uuid.UUID, payee_id: uuid.UUID) -> Payee | None:
        return await self.session.scalar(
            ScopeFilter.for_user(user_id).apply(select(Payee).where(Payee.id == payee_id), Payee)
        )

    async def categories_many(
        self, ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, TransactionCategory]:
        wanted = list(set(ids))
        if not wanted:
            return {}
        rows = await self.session.scalars(
            select(TransactionCategory).where(TransactionCategory.id.in_(wanted))
        )
        return {c.id: c for c in rows}

    async def payees_many(self, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Payee]:
        wanted = list(set(ids))
        if not wanted:
            return {}
        rows = await self.session.scalars(select(Payee).where(Payee.id.in_(wanted)))
        return {p.id: p for p in rows}

    async def user_names(self, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
        wanted = list(set(ids))
        if not wanted:
            return {}
        rows = await self.session.execute(
            select(User.id, User.full_name).where(User.id.in_(wanted))
        )
        return {uid: name for uid, name in rows}

    # ------------------------------------------------------------------ tags & attachments

    async def tags_for(self, tx_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
        out: dict[uuid.UUID, list[str]] = {i: [] for i in tx_ids}
        if not tx_ids:
            return out
        rows = await self.session.execute(
            select(TransactionTag.transaction_id, TransactionTag.name)
            .where(TransactionTag.transaction_id.in_(tx_ids))
            .order_by(TransactionTag.transaction_id, TransactionTag.name)
        )
        for tx_id, name in rows:
            out[tx_id].append(name)
        return out

    async def replace_tags(self, tx_id: uuid.UUID, tags: Sequence[str]) -> None:
        await self.session.execute(
            delete(TransactionTag).where(TransactionTag.transaction_id == tx_id)
        )
        if tags:
            await self.session.execute(
                insert(TransactionTag), [{"transaction_id": tx_id, "name": t} for t in tags]
            )

    async def attachment_counts(self, tx_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not tx_ids:
            return {}
        rows = await self.session.execute(
            select(TransactionAttachment.transaction_id, func.count())
            .where(
                TransactionAttachment.transaction_id.in_(tx_ids),
                TransactionAttachment.deleted_at.is_(None),
            )
            .group_by(TransactionAttachment.transaction_id)
        )
        return {tx_id: int(n) for tx_id, n in rows}

    # ------------------------------------------------------------------ filtering

    def filter_clauses(
        self,
        *,
        starts_at: datetime | None,
        ends_before: datetime | None,
        types: Sequence[TransactionType],
        category_ids: Sequence[uuid.UUID],
        account_id: uuid.UUID | None,
        payment_method: PaymentMethod | None,
        member_user_id: uuid.UUID | None,
        status: TransactionStatus | None,
        q: str | None,
        min_amount: Decimal | None,
        max_amount: Decimal | None,
        tag: str | None,
    ) -> list[ColumnElement[bool]]:
        """WHERE clauses on ``Transaction`` only (no joins), so the same list feeds the page
        query, the count, :func:`app.services.ledger.totals` and the CSV export."""
        t = Transaction
        where: list[ColumnElement[bool]] = []
        if starts_at is not None:
            where.append(t.occurred_at >= starts_at)
        if ends_before is not None:
            where.append(t.occurred_at < ends_before)
        if types:
            where.append(t.type.in_(list(types)))
        if category_ids:
            where.append(t.category_id.in_(self._category_tree(category_ids)))
        if account_id is not None:
            where.append(or_(t.account_id == account_id, t.to_account_id == account_id))
        if payment_method is not None:
            where.append(t.payment_method == payment_method)
        if member_user_id is not None:
            where.append(t.owner_user_id == member_user_id)
        if status is not None:
            where.append(t.status == status)
        if q:
            pattern = f"%{escape_like(q)}%"
            payee_ids = select(Payee.id).where(Payee.name.ilike(pattern, escape="\\"))
            where.append(
                or_(
                    t.description.ilike(pattern, escape="\\"),
                    t.merchant.ilike(pattern, escape="\\"),
                    t.notes.ilike(pattern, escape="\\"),
                    t.payee_id.in_(payee_ids),
                )
            )
        if min_amount is not None:
            where.append(t.amount >= min_amount)
        if max_amount is not None:
            where.append(t.amount <= max_amount)
        if tag:
            where.append(
                t.id.in_(
                    select(TransactionTag.transaction_id).where(TransactionTag.name == tag.lower())
                )
            )
        return where

    @staticmethod
    def _category_tree(category_ids: Sequence[uuid.UUID]) -> Select[tuple[uuid.UUID]]:
        """The given categories and all their descendants (recursive CTE)."""
        c = TransactionCategory
        tree = select(c.id).where(c.id.in_(list(category_ids))).cte("category_tree", recursive=True)
        child = aliased(c)
        tree = tree.union(select(child.id).where(child.parent_id == tree.c.id))
        return select(tree.c.id)

    async def page(
        self,
        where: Sequence[ColumnElement[bool]],
        *,
        sort_by: str,
        sort_dir: str,
        params: PageParams,
    ) -> tuple[list[Transaction], int]:
        key = Transaction.amount if sort_by == "amount" else Transaction.occurred_at
        direction = key.asc() if sort_dir == "asc" else key.desc()
        tiebreak = (
            (Transaction.created_at.asc(), Transaction.id.asc())
            if sort_dir == "asc"
            else (Transaction.created_at.desc(), Transaction.id.desc())
        )
        stmt = select(Transaction).where(*where).order_by(direction, *tiebreak)
        return await paginate(self.session, stmt, params)

    async def count(self, where: Sequence[ColumnElement[bool]]) -> int:
        n = await self.session.scalar(select(func.count()).select_from(Transaction).where(*where))
        return int(n or 0)

    async def stream_export(self, where: Sequence[ColumnElement[bool]]) -> AsyncIterator[Any]:
        """Flat rows for CSV export, newest first, streamed in batches."""
        src = aliased(Account)
        dst = aliased(Account)
        tags = (
            select(
                func.string_agg(
                    TransactionTag.name, aggregate_order_by(literal(" "), TransactionTag.name)
                )
            )
            .where(TransactionTag.transaction_id == Transaction.id)
            .correlate(Transaction)
            .scalar_subquery()
        )
        stmt = (
            select(
                Transaction,
                TransactionCategory.name,
                src.name,
                dst.name,
                Payee.name,
                User.full_name,
                tags,
            )
            .outerjoin(TransactionCategory, TransactionCategory.id == Transaction.category_id)
            .outerjoin(src, src.id == Transaction.account_id)
            .outerjoin(dst, dst.id == Transaction.to_account_id)
            .outerjoin(Payee, Payee.id == Transaction.payee_id)
            .join(User, User.id == Transaction.owner_user_id)
            .where(*where)
            .order_by(Transaction.occurred_at.desc(), Transaction.created_at.desc(), Transaction.id)
            .execution_options(yield_per=500)
        )
        result = await self.session.stream(stmt)
        async for row in result:
            yield row
