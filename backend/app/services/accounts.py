"""Accounts & payment methods (spec §7).

* Personal accounts: only their owner sees and manages them.
* Family accounts: every active member sees them; creating, editing, archiving and deleting
  needs ``FamilyPermission.MANAGE_SHARED_SETUP`` (owner/admin) — others get 403.
* ``current_balance`` is always computed by :func:`app.services.ledger.account_balances`
  (never stored).
* An account referenced by live transactions can't be deleted (409 ``account_in_use``);
  archive it instead (``is_active=false`` hides it from pickers but keeps history).
* No credentials of any kind are accepted or stored.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.core.permissions import (
    FamilyPermission,
    ScopeFilter,
    list_user_family_roles,
    require_family_access,
    role_has,
)
from app.core.security import utcnow
from app.models.enums import FamilyRole, RecordScope
from app.models.finance import Account
from app.models.user import User
from app.repositories.accounts import AccountRepository
from app.repositories.system import AuditRepository
from app.schemas.accounts import AccountCreate, AccountOut, AccountUpdate
from app.services import ledger
from app.services.auth import ClientInfo


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal | uuid.UUID):
        return str(value)
    return value


class AccountService:
    def __init__(self, session: AsyncSession) -> None:
        self.db = session
        self.accounts = AccountRepository(session)
        self.audit = AuditRepository(session)

    # ------------------------------------------------------------------ helpers

    def _audit(
        self, action: str, user: User, account: Account, client: ClientInfo, **details: Any
    ) -> None:
        self.audit.record(
            action,
            actor_user_id=user.id,
            entity_type="account",
            entity_id=account.id,
            family_id=account.family_id,
            ip_address=client.ip,
            user_agent=client.user_agent,
            details={k: _jsonable(v) for k, v in details.items()},
        )

    @staticmethod
    def _out(account: Account, balance: Decimal, can_manage: bool) -> AccountOut:
        return AccountOut(
            id=account.id,
            name=account.name,
            type=account.type,
            institution=account.institution,
            currency=account.currency,
            opening_balance=account.opening_balance,
            current_balance=balance,
            scope=account.scope,
            family_id=account.family_id,
            owner_user_id=account.owner_user_id,
            is_active=account.is_active,
            can_manage=can_manage,
            created_at=account.created_at,
            updated_at=account.updated_at,
        )

    async def _detail(self, user: User, account: Account) -> AccountOut:
        balances = await ledger.account_balances(self.db, [account.id])
        roles = await list_user_family_roles(self.db, user.id)
        return self._out(account, balances[account.id], _can_manage(account, roles))

    async def _visible(self, user: User, account_id: uuid.UUID, *, for_update: bool) -> Account:
        account = await self.accounts.get_visible(user.id, account_id, for_update=for_update)
        if account is None:
            raise NotFoundError("Account not found")
        return account

    async def _manageable(self, user: User, account_id: uuid.UUID) -> Account:
        """Visible (else 404) and manageable by ``user`` (else 403), row-locked."""
        account = await self._visible(user, account_id, for_update=True)
        if account.scope is RecordScope.FAMILY and account.family_id is not None:
            await require_family_access(
                self.db, user, account.family_id, FamilyPermission.MANAGE_SHARED_SETUP
            )
        return account

    # ------------------------------------------------------------------ reads

    async def list_accounts(
        self, user: User, family_id: uuid.UUID | None, *, include_inactive: bool
    ) -> list[AccountOut]:
        """Personal accounts (no ``family_id``) or that family's accounts (member only)."""
        if family_id is not None:
            await require_family_access(self.db, user, family_id)
            scope = ScopeFilter.for_user(user.id, family_id=family_id)
        else:
            scope = ScopeFilter.for_user(user.id, scope=RecordScope.PERSONAL)
        rows = await self.accounts.list_visible(scope, include_inactive=include_inactive)
        balances = await ledger.account_balances(self.db, [a.id for a in rows])
        roles = await list_user_family_roles(self.db, user.id)
        return [self._out(a, balances[a.id], _can_manage(a, roles)) for a in rows]

    async def get_account(self, user: User, account_id: uuid.UUID) -> AccountOut:
        return await self._detail(user, await self._visible(user, account_id, for_update=False))

    # ------------------------------------------------------------------ writes

    async def create_account(
        self, user: User, data: AccountCreate, client: ClientInfo
    ) -> AccountOut:
        if data.scope is RecordScope.FAMILY and data.family_id is not None:
            await require_family_access(
                self.db, user, data.family_id, FamilyPermission.MANAGE_SHARED_SETUP
            )
        account = Account(
            owner_user_id=user.id,
            scope=data.scope,
            family_id=data.family_id,
            name=data.name,
            type=data.type,
            institution=data.institution,
            opening_balance=data.opening_balance,
            currency=data.currency,
            is_active=True,
        )
        self.accounts.add(account)
        await self.db.flush()
        self._audit(
            "account.created",
            user,
            account,
            client,
            name=account.name,
            type=account.type.value,
            scope=account.scope.value,
            opening_balance=account.opening_balance,
            currency=account.currency,
        )
        await self.db.commit()
        await self.db.refresh(account)
        return await self._detail(user, account)

    async def update_account(
        self, user: User, account_id: uuid.UUID, data: AccountUpdate, client: ClientInfo
    ) -> AccountOut:
        account = await self._manageable(user, account_id)
        changes: dict[str, dict[str, Any]] = {}
        for field, value in data.model_dump(exclude_unset=True).items():
            before = getattr(account, field)
            if before != value:
                changes[field] = {"before": _jsonable(before), "after": _jsonable(value)}
                setattr(account, field, value)
        if changes:
            self._audit("account.updated", user, account, client, changes=changes)
            await self.db.commit()
            await self.db.refresh(account)
        return await self._detail(user, account)

    async def set_active(
        self, user: User, account_id: uuid.UUID, active: bool, client: ClientInfo
    ) -> AccountOut:
        """Archive (``active=False``) or unarchive an account."""
        account = await self._manageable(user, account_id)
        if account.is_active != active:
            account.is_active = active
            self._audit(
                "account.unarchived" if active else "account.archived", user, account, client
            )
            await self.db.commit()
            await self.db.refresh(account)
        return await self._detail(user, account)

    async def delete_account(self, user: User, account_id: uuid.UUID, client: ClientInfo) -> None:
        account = await self._manageable(user, account_id)
        if await self.accounts.has_live_transactions(account.id):
            raise ConflictError(
                "This account has transactions. Archive it instead, or delete its "
                "transactions first.",
                code="account_in_use",
            )
        account.deleted_at = utcnow()
        self._audit("account.deleted", user, account, client, name=account.name)
        await self.db.commit()


def _can_manage(account: Account, roles: dict[uuid.UUID, FamilyRole]) -> bool:
    if account.scope is RecordScope.PERSONAL:
        return True
    role = roles.get(account.family_id) if account.family_id else None
    return role is not None and role_has(role, FamilyPermission.MANAGE_SHARED_SETUP)
