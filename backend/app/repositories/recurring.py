"""Queries for recurring rules and the transactions they generate.

Visibility is always applied with :class:`app.core.permissions.ScopeFilter`; the worker-only
queries (``due_rule_ids`` / ``lock_for_generation`` / reminder scans) run without a user and
instead require a live owner and, for family rules, a live family.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import ScopeFilter, visible_categories_clause
from app.models.enums import FamilyRole, RecordScope, TransactionSource, TransactionStatus
from app.models.family import Family, FamilyMember
from app.models.finance import Account, Payee, Transaction, TransactionCategory
from app.models.planning import RecurringTransaction
from app.models.user import User

RT = RecurringTransaction


def _live_owner_and_family() -> list[Any]:
    """Join conditions for rules the worker may act on: owner active; for a family rule, the
    family is live and its owner is still an active member allowed to create family records
    (a rule whose creator left the family or became a viewer stops generating/reminding)."""
    owner_can_create = (
        select(FamilyMember.id)
        .where(
            FamilyMember.family_id == RT.family_id,
            FamilyMember.user_id == RT.owner_user_id,
            FamilyMember.left_at.is_(None),
            FamilyMember.role.in_([FamilyRole.OWNER, FamilyRole.ADMIN, FamilyRole.MEMBER]),
        )
        .exists()
    )
    return [
        User.deleted_at.is_(None),
        User.is_active.is_(True),
        or_(
            RT.scope == RecordScope.PERSONAL,
            and_(Family.deleted_at.is_(None), owner_can_create),
        ),
    ]


def _worker_select(*entities: Any) -> Select[Any]:
    return (
        select(*entities)
        .select_from(RT)
        .join(User, User.id == RT.owner_user_id)
        .outerjoin(Family, Family.id == RT.family_id)
        .where(RT.deleted_at.is_(None), *_live_owner_and_family())
    )


class RecurringRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, rule: RecurringTransaction) -> None:
        self.session.add(rule)

    # ------------------------------------------------------------------ rules

    async def get(self, rule_id: uuid.UUID) -> RecurringTransaction | None:
        """Raw row-locked lookup (no visibility) — pass to ``require_record_write`` which
        404s. Locked so concurrent edits / pause / resume of one rule serialise."""
        return await self.session.get(RT, rule_id, with_for_update=True)

    async def get_visible(
        self, user_id: uuid.UUID, rule_id: uuid.UUID
    ) -> RecurringTransaction | None:
        stmt = ScopeFilter.for_user(user_id).apply(select(RT).where(RT.id == rule_id), RT)
        return await self.session.scalar(stmt)

    def list_stmt(
        self,
        scope: ScopeFilter,
        *,
        status: str | None,
    ) -> Select[RecurringTransaction]:
        stmt = scope.apply(select(RT), RT)
        ended = and_(RT.end_date.is_not(None), RT.next_due_date > RT.end_date)
        if status == "paused":
            stmt = stmt.where(RT.is_paused.is_(True))
        elif status == "ended":
            stmt = stmt.where(RT.is_paused.is_(False), ended)
        elif status == "active":
            stmt = stmt.where(RT.is_paused.is_(False), ~ended)
        return stmt.order_by(RT.next_due_date, RT.name, RT.id)

    async def rules_for_scope(self, scope: ScopeFilter) -> Sequence[RecurringTransaction]:
        """Every live rule (paused included) visible through ``scope``."""
        stmt = scope.apply(select(RT), RT).order_by(RT.id)
        return list(await self.session.scalars(stmt))

    async def owner_timezone(self, user_id: uuid.UUID) -> str | None:
        return await self.session.scalar(select(User.timezone).where(User.id == user_id))

    # ------------------------------------------------------------------ occurrences

    async def latest_occurrences(
        self, rule_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, Transaction]:
        """The live generated transaction with the latest period for each rule."""
        if not rule_ids:
            return {}
        live = and_(Transaction.recurring_id.in_(rule_ids), Transaction.deleted_at.is_(None))
        latest = (
            select(
                Transaction.recurring_id.label("rid"),
                func.max(Transaction.period_key).label("pk"),
            )
            .where(live)
            .group_by(Transaction.recurring_id)
            .subquery()
        )
        stmt = select(Transaction).join(
            latest,
            and_(Transaction.recurring_id == latest.c.rid, Transaction.period_key == latest.c.pk),
        )
        return {t.recurring_id: t for t in await self.session.scalars(stmt) if t.recurring_id}

    def occurrences_stmt(self, rule_id: uuid.UUID) -> Select[Transaction]:
        return (
            select(Transaction)
            .where(Transaction.recurring_id == rule_id, Transaction.deleted_at.is_(None))
            .order_by(Transaction.period_key.desc(), Transaction.id)
        )

    async def occurrences_in_range(
        self, rule_ids: Sequence[uuid.UUID], first_key: str, last_key: str
    ) -> dict[tuple[uuid.UUID, str], Transaction]:
        """Generated transactions (soft-deleted included) keyed by (rule id, period key)."""
        if not rule_ids:
            return {}
        stmt = select(Transaction).where(
            Transaction.recurring_id.in_(rule_ids),
            Transaction.period_key >= first_key,
            Transaction.period_key <= last_key,
        )
        return {
            (t.recurring_id, t.period_key): t
            for t in await self.session.scalars(stmt)
            if t.recurring_id is not None and t.period_key is not None
        }

    async def pending_before(
        self, rule_ids: Sequence[uuid.UUID], before_key: str
    ) -> Sequence[Transaction]:
        """Live generated transactions still pending whose period is before ``before_key``."""
        if not rule_ids:
            return []
        stmt = (
            select(Transaction)
            .where(
                Transaction.recurring_id.in_(rule_ids),
                Transaction.deleted_at.is_(None),
                Transaction.status == TransactionStatus.PENDING,
                Transaction.period_key < before_key,
            )
            .order_by(Transaction.period_key)
        )
        return list(await self.session.scalars(stmt))

    async def latest_period_key(self, rule_id: uuid.UUID) -> str | None:
        """Latest period ever generated for the rule (soft-deleted rows included)."""
        return await self.session.scalar(
            select(func.max(Transaction.period_key)).where(Transaction.recurring_id == rule_id)
        )

    async def get_generated_transaction(self, transaction_id: uuid.UUID) -> Transaction | None:
        """Row-locked (status changes from concurrent requests serialise)."""
        return await self.session.scalar(
            select(Transaction)
            .where(
                Transaction.id == transaction_id,
                Transaction.recurring_id.is_not(None),
                Transaction.source == TransactionSource.RECURRING,
            )
            .with_for_update()
        )

    async def insert_occurrence(self, values: dict[str, Any]) -> uuid.UUID | None:
        """INSERT … ON CONFLICT (recurring_id, period_key) DO NOTHING. Returns the new id, or
        ``None`` when that period already exists (another worker / an earlier run made it)."""
        stmt = (
            pg_insert(Transaction)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["recurring_id", "period_key"])
            .returning(Transaction.id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # ------------------------------------------------------------------ worker

    async def due_rule_ids(self, horizon: date) -> list[uuid.UUID]:
        """Rules that may have something to process up to ``horizon`` (a coarse filter; the
        exact per-owner-timezone check happens under the row lock)."""
        stmt = _worker_select(RT.id).where(
            RT.is_paused.is_(False),
            RT.next_due_date <= horizon,
            or_(RT.end_date.is_(None), RT.next_due_date <= RT.end_date),
        )
        return list(await self.session.scalars(stmt.order_by(RT.next_due_date, RT.id)))

    async def lock_for_generation(
        self, rule_id: uuid.UUID
    ) -> tuple[RecurringTransaction, str] | None:
        """Row-lock a still-eligible rule (``SKIP LOCKED``: a rule another worker is processing
        right now is skipped — the unique (recurring_id, period_key) still guards inserts)."""
        stmt = (
            _worker_select(RT, User.timezone)
            .where(RT.id == rule_id, RT.is_paused.is_(False))
            .with_for_update(of=RT, skip_locked=True)
            .execution_options(populate_existing=True)
        )
        row = (await self.session.execute(stmt)).first()
        return (row[0], row[1]) if row is not None else None

    async def reminder_rules(self) -> Sequence[tuple[RecurringTransaction, str]]:
        """Live, unpaused, not-ended rules (owner/family live) with the owner's time zone."""
        stmt = _worker_select(RT, User.timezone).where(
            RT.is_paused.is_(False),
            or_(RT.end_date.is_(None), RT.next_due_date <= RT.end_date),
        )
        return [(r, tz) for r, tz in await self.session.execute(stmt.order_by(RT.id))]

    async def overdue_candidates(
        self, since_key: str, before_key: str
    ) -> Sequence[tuple[Transaction, RecurringTransaction, str]]:
        """Pending generated transactions with ``since_key <= period_key < before_key`` of live
        rules (owner/family live)."""
        stmt = (
            _worker_select(Transaction, RT, User.timezone)
            .join(Transaction, Transaction.recurring_id == RT.id)
            .where(
                Transaction.deleted_at.is_(None),
                Transaction.status == TransactionStatus.PENDING,
                Transaction.period_key >= since_key,
                Transaction.period_key < before_key,
            )
            .order_by(Transaction.period_key, Transaction.id)
        )
        return [(t, r, tz) for t, r, tz in await self.session.execute(stmt)]

    async def reminder_recipients(self, rule: RecurringTransaction) -> list[uuid.UUID]:
        """Personal rule → its owner. Family rule → the rule's owner (if still an active member)
        plus the family's owner and admins."""
        if rule.scope is RecordScope.PERSONAL or rule.family_id is None:
            return [rule.owner_user_id]
        stmt = (
            select(FamilyMember.user_id)
            .join(User, User.id == FamilyMember.user_id)
            .where(
                FamilyMember.family_id == rule.family_id,
                FamilyMember.left_at.is_(None),
                User.deleted_at.is_(None),
                or_(
                    FamilyMember.user_id == rule.owner_user_id,
                    FamilyMember.role.in_([FamilyRole.OWNER, FamilyRole.ADMIN]),
                ),
            )
            .order_by(FamilyMember.user_id)
        )
        return list(dict.fromkeys(await self.session.scalars(stmt)))

    # ------------------------------------------------------------------ references

    async def account_in_scope(
        self, account_id: uuid.UUID, user_id: uuid.UUID, family_id: uuid.UUID | None
    ) -> Account | None:
        scope = (
            ScopeFilter.for_user(user_id, family_id=family_id)
            if family_id is not None
            else ScopeFilter.for_user(user_id, scope=RecordScope.PERSONAL)
        )
        return await self.session.scalar(
            scope.apply(select(Account).where(Account.id == account_id), Account)
        )

    async def payee_in_scope(
        self, payee_id: uuid.UUID, user_id: uuid.UUID, family_id: uuid.UUID | None
    ) -> Payee | None:
        scope = (
            ScopeFilter.for_user(user_id, family_id=family_id)
            if family_id is not None
            else ScopeFilter.for_user(user_id, scope=RecordScope.PERSONAL)
        )
        return await self.session.scalar(
            scope.apply(select(Payee).where(Payee.id == payee_id), Payee)
        )

    async def category_in_scope(
        self, category_id: uuid.UUID, user_id: uuid.UUID, family_id: uuid.UUID | None
    ) -> TransactionCategory | None:
        c = TransactionCategory
        if family_id is not None:
            visible = visible_categories_clause(user_id, family_id=family_id)
        else:  # personal record: built-ins + my personal categories
            visible = or_(
                and_(c.owner_user_id.is_(None), c.family_id.is_(None)),
                c.owner_user_id == user_id,
            )
        return await self.session.scalar(
            select(c).where(c.id == category_id, c.deleted_at.is_(None), visible)
        )

    async def category_archived(self, category: TransactionCategory) -> bool:
        """Archived itself, or a subcategory of an archived parent."""
        if category.is_archived:
            return True
        if category.parent_id is None:
            return False
        return bool(
            await self.session.scalar(
                select(TransactionCategory.is_archived).where(
                    TransactionCategory.id == category.parent_id
                )
            )
        )

    async def family_currency(self, family_id: uuid.UUID) -> str | None:
        return await self.session.scalar(select(Family.currency).where(Family.id == family_id))
