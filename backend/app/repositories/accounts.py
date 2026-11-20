"""Queries for accounts. Visibility always goes through :class:`ScopeFilter`."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import ScopeFilter
from app.models.finance import Account, Transaction


class AccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, account: Account) -> None:
        self.session.add(account)

    async def get_visible(
        self, user_id: uuid.UUID, account_id: uuid.UUID, *, for_update: bool = False
    ) -> Account | None:
        """A live account ``user_id`` may see (own personal or one of their families')."""
        stmt = ScopeFilter.for_user(user_id).apply(
            select(Account).where(Account.id == account_id), Account
        )
        if for_update:
            stmt = stmt.with_for_update(of=Account)
        return await self.session.scalar(stmt)

    async def list_visible(self, scope: ScopeFilter, *, include_inactive: bool) -> list[Account]:
        stmt = scope.apply(select(Account), Account)
        if not include_inactive:
            stmt = stmt.where(Account.is_active.is_(True))
        stmt = stmt.order_by(Account.is_active.desc(), Account.name, Account.created_at, Account.id)
        return list(await self.session.scalars(stmt))

    async def get_many(self, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Account]:
        """Accounts by id, *without* visibility filtering (use only for rows already
        authorised, e.g. the accounts referenced by a visible transaction)."""
        wanted = list(set(ids))
        if not wanted:
            return {}
        rows = await self.session.scalars(select(Account).where(Account.id.in_(wanted)))
        return {a.id: a for a in rows}

    async def has_live_transactions(self, account_id: uuid.UUID) -> bool:
        found = await self.session.scalar(
            select(Transaction.id)
            .where(
                Transaction.deleted_at.is_(None),
                or_(Transaction.account_id == account_id, Transaction.to_account_id == account_id),
            )
            .limit(1)
        )
        return found is not None
