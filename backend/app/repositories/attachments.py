"""Queries for transaction attachments (receipts)."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import ScopeFilter
from app.models.finance import Transaction, TransactionAttachment

A = TransactionAttachment


class AttachmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, attachment: TransactionAttachment) -> None:
        self.session.add(attachment)

    async def get_live_transaction(
        self, transaction_id: uuid.UUID, *, for_update: bool = False
    ) -> Transaction | None:
        """A non-deleted transaction by id — no visibility check (use ``require_record_write``)."""
        stmt = select(Transaction).where(
            Transaction.id == transaction_id, Transaction.deleted_at.is_(None)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt)

    async def get_visible_transaction(
        self, user_id: uuid.UUID, transaction_id: uuid.UUID
    ) -> Transaction | None:
        stmt = ScopeFilter.for_user(user_id).apply(
            select(Transaction).where(Transaction.id == transaction_id), Transaction
        )
        return await self.session.scalar(stmt)

    async def count_live(self, transaction_id: uuid.UUID) -> int:
        count = await self.session.scalar(
            select(func.count())
            .select_from(A)
            .where(A.transaction_id == transaction_id, A.deleted_at.is_(None))
        )
        return int(count or 0)

    async def list_live(self, transaction_id: uuid.UUID) -> list[TransactionAttachment]:
        rows = await self.session.scalars(
            select(A)
            .where(A.transaction_id == transaction_id, A.deleted_at.is_(None))
            .order_by(A.created_at, A.id)
        )
        return list(rows)

    async def get_live(
        self, transaction_id: uuid.UUID, attachment_id: uuid.UUID
    ) -> TransactionAttachment | None:
        return await self.session.scalar(
            select(A).where(
                A.id == attachment_id,
                A.transaction_id == transaction_id,
                A.deleted_at.is_(None),
            )
        )
