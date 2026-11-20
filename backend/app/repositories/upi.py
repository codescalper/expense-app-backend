"""Queries for UPI payment attempts. Visibility always goes through ``ScopeFilter``."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import ScopeFilter
from app.models.enums import RecordScope, TransactionStatus, UpiAttemptStatus
from app.models.family import Family, FamilyMember
from app.models.finance import Payee, Transaction
from app.models.upi import UpiPaymentAttempt
from app.models.user import User
from app.repositories.categories import escape_like
from app.utils.pagination import PageParams

A = UpiPaymentAttempt


class UpiRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, attempt: UpiPaymentAttempt) -> None:
        self.session.add(attempt)

    async def lock_idempotency_key(self, owner_id: uuid.UUID, key: str) -> None:
        """Transaction-scoped advisory lock serialising initiations that share one key, so
        concurrent retries wait for the first one and then see its committed attempt."""
        await self.session.execute(
            select(func.pg_advisory_xact_lock(func.hashtext(f"upi:{owner_id}:{key}")))
        )

    async def get_by_idempotency_key(self, owner_id: uuid.UUID, key: str) -> A | None:
        return await self.session.scalar(
            select(A).where(A.owner_user_id == owner_id, A.idempotency_key == key)
        )

    async def get_visible(
        self, user_id: uuid.UUID, attempt_id: uuid.UUID, *, for_update: bool = False
    ) -> A | None:
        stmt = ScopeFilter.for_user(user_id).apply(select(A).where(A.id == attempt_id), A)
        if for_update:
            stmt = stmt.with_for_update(of=A)
        return await self.session.scalar(stmt)

    # --- lists --------------------------------------------------------------------------

    @staticmethod
    def filter_clauses(
        *,
        statuses: Sequence[UpiAttemptStatus] = (),
        q: str | None = None,
        starts_at: datetime | None = None,
        ends_before: datetime | None = None,
    ) -> list[ColumnElement[bool]]:
        clauses: list[ColumnElement[bool]] = []
        if statuses:
            clauses.append(A.status.in_(list(statuses)))
        if q:
            pattern = f"%{escape_like(q)}%"
            clauses.append(
                or_(
                    A.payee_name.ilike(pattern, escape="\\"),
                    A.payee_vpa.ilike(pattern, escape="\\"),
                    A.note.ilike(pattern, escape="\\"),
                    A.utr.ilike(pattern, escape="\\"),
                    A.tr_ref.ilike(pattern, escape="\\"),
                )
            )
        if starts_at is not None:
            clauses.append(A.initiated_at >= starts_at)
        if ends_before is not None:
            clauses.append(A.initiated_at < ends_before)
        return clauses

    async def page(
        self, where: Sequence[ColumnElement[bool]], params: PageParams
    ) -> tuple[list[A], int]:
        total = int(
            (
                await self.session.execute(select(func.count()).select_from(A).where(*where))
            ).scalar_one()
        )
        rows = await self.session.scalars(
            select(A)
            .where(*where)
            .order_by(A.initiated_at.desc(), A.id.desc())
            .limit(params.limit)
            .offset(params.offset)
        )
        return list(rows), total

    # --- output helpers -----------------------------------------------------------------

    async def user_names(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not ids:
            return {}
        rows = await self.session.execute(
            select(User.id, User.full_name).where(User.id.in_(set(ids)))
        )
        return {uid: name for uid, name in rows}

    async def transaction_statuses(
        self, ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, TransactionStatus]:
        """Status of each *live* transaction (soft-deleted ones are left out)."""
        if not ids:
            return {}
        rows = await self.session.execute(
            select(Transaction.id, Transaction.status).where(
                Transaction.id.in_(set(ids)), Transaction.deleted_at.is_(None)
            )
        )
        return {tid: status for tid, status in rows}

    async def get_payee(self, payee_id: uuid.UUID | None) -> Payee | None:
        return await self.session.get(Payee, payee_id) if payee_id else None

    async def get_transaction(
        self, tx_id: uuid.UUID | None, *, for_update: bool = False
    ) -> Transaction | None:
        if tx_id is None:
            return None
        stmt = select(Transaction).where(Transaction.id == tx_id)
        if for_update:
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt)

    # --- reminders ----------------------------------------------------------------------

    async def reminder_candidates(
        self, *, initiated_after: datetime, initiated_before: datetime, limit: int
    ) -> list[A]:
        """Open attempts (initiated/launched/pending_reported) initiated in the window whose
        transaction is still live and pending, and whose initiator can still see them."""
        still_member = (
            select(FamilyMember.family_id)
            .join(Family, Family.id == FamilyMember.family_id)
            .where(
                FamilyMember.family_id == A.family_id,
                FamilyMember.user_id == A.owner_user_id,
                FamilyMember.left_at.is_(None),
                Family.deleted_at.is_(None),
            )
            .exists()
        )
        stmt = (
            select(A)
            .join(Transaction, Transaction.id == A.transaction_id)
            .join(User, User.id == A.owner_user_id)
            .where(
                A.status.in_(
                    [
                        UpiAttemptStatus.INITIATED,
                        UpiAttemptStatus.LAUNCHED,
                        UpiAttemptStatus.PENDING_REPORTED,
                    ]
                ),
                A.initiated_at > initiated_after,
                A.initiated_at <= initiated_before,
                Transaction.deleted_at.is_(None),
                Transaction.status == TransactionStatus.PENDING,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
                or_(
                    A.scope == RecordScope.PERSONAL,
                    and_(A.scope == RecordScope.FAMILY, still_member),
                ),
            )
            .order_by(A.initiated_at, A.id)
            .limit(limit)
        )
        return list(await self.session.scalars(stmt))
