"""Savings goals (spec §10): goals, contributions, progress, pacing, history, milestones.

* ``current_amount`` = live deposits - live withdrawals (computed, never stored).
* A withdrawal (or deleting a deposit) can never take the balance below zero → 409.
* Status: an ``active`` goal whose balance reaches the target becomes ``completed``
  (``completed_at`` set); a ``completed`` goal whose balance drops below the target goes back to
  ``active``. ``archived`` is only set/cleared by the user and freezes contributions.
* Milestones at 25/50/75/100 % notify the owner (personal goal) or every active family member
  (family goal) once per goal (dedupe key ``goal:<id>:<pct>``).

Permissions: personal goals → owner only. Family goals: any member with ``CREATE_RECORDS``
(owner/admin/member) may create goals and add contributions; editing/deleting a goal follows
``require_record_write`` (owner/admin any goal, members their own); deleting a contribution is
allowed to its author (if still allowed to create records) or to owner/admins.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationFailedError
from app.core.permissions import (
    FamilyPermission,
    get_active_membership,
    list_user_family_roles,
    require_family_access,
    require_record_write,
    role_has,
)
from app.core.security import utcnow
from app.models.enums import (
    ContributionKind,
    FamilyRole,
    GoalStatus,
    NotificationCategory,
    RecordScope,
)
from app.models.family import Family
from app.models.planning import SavingsContribution, SavingsGoal
from app.models.user import User
from app.repositories.savings import SavingsRepository
from app.repositories.system import AuditRepository
from app.schemas.savings import (
    MILESTONES,
    ContributionCreate,
    ContributionCreatedOut,
    ContributionOut,
    GoalCreate,
    GoalHistoryOut,
    GoalHistoryPoint,
    GoalOut,
    GoalUpdate,
    PacingOut,
)
from app.services.auth import ClientInfo
from app.services.budgets import zone
from app.services.notify import notify, notify_family_members
from app.utils.pagination import Page, PageParams, paginate

ZERO = Decimal("0.00")
CENT = Decimal("0.01")
FUTURE_SKEW = timedelta(days=1)


def progress_percent(current: Decimal, target: Decimal) -> float:
    if target <= 0:
        return 0.0
    pct = max(current, ZERO) * 100 / target
    return float(pct.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def months_between(today: date, target: date) -> int:
    """Whole months from ``today`` until ``target`` (minimum 1)."""
    months = (target.year - today.year) * 12 + (target.month - today.month)
    if target.day < today.day:
        months -= 1
    return max(months, 1)


def pacing(target_date: date | None, remaining: Decimal, today: date) -> PacingOut | None:
    if target_date is None or remaining <= 0:
        return None
    overdue = target_date < today
    months = 1 if overdue else months_between(today, target_date)
    return PacingOut(
        target_date=target_date,
        days_left=max((target_date - today).days, 0),
        months_left=months,
        required_monthly=(remaining / months).quantize(CENT, rounding=ROUND_UP),
        overdue=overdue,
    )


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _next_month(key: str) -> str:
    year, month = (int(p) for p in key.split("-"))
    return f"{year + 1:04d}-01" if month == 12 else f"{year:04d}-{month + 1:02d}"


class SavingsService:
    def __init__(self, session: AsyncSession) -> None:
        self.db = session
        self.repo = SavingsRepository(session)
        self.audit = AuditRepository(session)

    # ------------------------------------------------------------------ helpers

    async def _visible(
        self, user: User, goal_id: uuid.UUID, *, for_update: bool = False
    ) -> SavingsGoal:
        goal = await self.repo.get_visible(user.id, goal_id, for_update=for_update)
        if goal is None:
            raise NotFoundError("Savings goal not found")
        return goal

    async def _require_contribute(self, user: User, goal: SavingsGoal) -> None:
        if goal.scope is RecordScope.FAMILY:
            if goal.family_id is None:  # pragma: no cover - enforced by ck_*_scope_family
                raise NotFoundError()
            await require_family_access(
                self.db, user, goal.family_id, FamilyPermission.CREATE_RECORDS
            )

    @staticmethod
    def _can_edit(user: User, goal: SavingsGoal, roles: dict[uuid.UUID, FamilyRole]) -> bool:
        if goal.scope is RecordScope.PERSONAL:
            return goal.owner_user_id == user.id
        role = roles.get(goal.family_id) if goal.family_id else None
        if role is None:
            return False
        if role_has(role, FamilyPermission.MANAGE_ALL_RECORDS):
            return True
        return role_has(role, FamilyPermission.CREATE_RECORDS) and goal.owner_user_id == user.id

    @staticmethod
    def _can_delete_contribution(
        user: User,
        goal: SavingsGoal,
        contribution: SavingsContribution,
        roles: dict[uuid.UUID, FamilyRole],
    ) -> bool:
        """Mirrors :meth:`delete_contribution`'s permission check."""
        if goal.scope is RecordScope.PERSONAL:
            return goal.owner_user_id == user.id
        role = roles.get(goal.family_id) if goal.family_id else None
        if role is None:
            return False
        return role_has(role, FamilyPermission.MANAGE_ALL_RECORDS) or (
            role_has(role, FamilyPermission.CREATE_RECORDS) and contribution.user_id == user.id
        )

    async def _contributions_out(
        self, user: User, goal: SavingsGoal, rows: list[SavingsContribution]
    ) -> list[ContributionOut]:
        if not rows:
            return []
        roles = await list_user_family_roles(self.db, user.id)
        names = await self.repo.user_names([r.user_id for r in rows])
        return [
            ContributionOut(
                id=r.id,
                goal_id=r.goal_id,
                user_id=r.user_id,
                user_name=names.get(r.user_id, ""),
                kind=r.kind,
                amount=r.amount,
                note=r.note,
                contributed_at=r.contributed_at,
                transaction_id=r.transaction_id,
                can_delete=self._can_delete_contribution(user, goal, r, roles),
                created_at=r.created_at,
            )
            for r in rows
        ]

    @staticmethod
    def _can_contribute(user: User, goal: SavingsGoal, roles: dict[uuid.UUID, FamilyRole]) -> bool:
        if goal.scope is RecordScope.PERSONAL:
            return goal.owner_user_id == user.id
        role = roles.get(goal.family_id) if goal.family_id else None
        return role is not None and role_has(role, FamilyPermission.CREATE_RECORDS)

    def _audit(
        self,
        action: str,
        user: User,
        goal: SavingsGoal,
        client: ClientInfo,
        *,
        entity_type: str = "savings_goal",
        entity_id: uuid.UUID | None = None,
        **details: object,
    ) -> None:
        self.audit.record(
            action,
            actor_user_id=user.id,
            entity_type=entity_type,
            entity_id=entity_id or goal.id,
            family_id=goal.family_id,
            ip_address=client.ip,
            user_agent=client.user_agent,
            details=dict(details),
        )

    async def _out_many(self, user: User, goals: list[SavingsGoal], now: datetime) -> list[GoalOut]:
        roles = await list_user_family_roles(self.db, user.id)
        balances = await self.repo.balances([g.id for g in goals])
        tzs = await self.repo.user_timezones([g.owner_user_id for g in goals])
        out = []
        for g in goals:
            current = balances[g.id]
            remaining = max(g.target_amount - current, ZERO)
            today = now.astimezone(zone(tzs.get(g.owner_user_id))).date()
            out.append(
                GoalOut(
                    id=g.id,
                    scope=g.scope,
                    family_id=g.family_id,
                    owner_user_id=g.owner_user_id,
                    name=g.name,
                    target_amount=g.target_amount,
                    current_amount=current,
                    remaining_amount=remaining,
                    progress_percent=progress_percent(current, g.target_amount),
                    target_date=g.target_date,
                    currency=g.currency,
                    status=g.status,
                    icon=g.icon,
                    color=g.color,
                    completed_at=g.completed_at,
                    pacing=(
                        pacing(g.target_date, remaining, today)
                        if g.status is GoalStatus.ACTIVE
                        else None
                    ),
                    can_edit=self._can_edit(user, g, roles),
                    can_contribute=self._can_contribute(user, g, roles),
                    created_at=g.created_at,
                    updated_at=g.updated_at,
                )
            )
        return out

    async def _out(self, user: User, goal: SavingsGoal, now: datetime) -> GoalOut:
        return (await self._out_many(user, [goal], now))[0]

    async def _settle(self, goal: SavingsGoal, now: datetime) -> Decimal:
        """Apply automatic status changes and milestone notifications after the balance or
        target changed. Returns the current balance. Caller commits."""
        balance = (await self.repo.balances([goal.id]))[goal.id]
        if goal.status is GoalStatus.ACTIVE and balance >= goal.target_amount:
            goal.status = GoalStatus.COMPLETED
            goal.completed_at = now
        elif goal.status is GoalStatus.COMPLETED and balance < goal.target_amount:
            goal.status = GoalStatus.ACTIVE
            goal.completed_at = None
        if goal.status is not GoalStatus.ARCHIVED:
            await self._milestones(goal, balance)
        return balance

    async def _milestones(self, goal: SavingsGoal, balance: Decimal) -> None:
        for pct in MILESTONES:
            if balance * 100 < goal.target_amount * pct:
                break
            title = f"Goal reached: {goal.name}" if pct == 100 else f"{goal.name}: {pct}% saved"
            body = (
                f"{goal.name} has reached its target of {goal.target_amount:,.2f} {goal.currency}."
                if pct == 100
                else f"{goal.name} is {pct}% of the way to {goal.target_amount:,.2f} "
                f"{goal.currency}."
            )
            data = {"goal_id": str(goal.id), "milestone": pct}
            key = f"goal:{goal.id}:{pct}"
            if goal.scope is RecordScope.FAMILY:
                if goal.family_id is None:  # pragma: no cover - enforced by ck_*_scope_family
                    raise NotFoundError()
                await notify_family_members(
                    self.db,
                    family_id=goal.family_id,
                    category=NotificationCategory.SAVINGS_MILESTONE,
                    title=title,
                    body=body,
                    dedupe_key=key,
                    data=data,
                )
            else:
                await notify(
                    self.db,
                    user_id=goal.owner_user_id,
                    category=NotificationCategory.SAVINGS_MILESTONE,
                    title=title,
                    body=body,
                    dedupe_key=key,
                    data=data,
                )

    # ------------------------------------------------------------------ goals

    async def create_goal(
        self, user: User, data: GoalCreate, client: ClientInfo, now: datetime | None = None
    ) -> GoalOut:
        now = now or utcnow()
        currency = data.currency or "INR"
        if data.scope is RecordScope.FAMILY:
            if data.family_id is None:  # pragma: no cover - enforced by ck_*_scope_family
                raise NotFoundError()
            await require_family_access(
                self.db, user, data.family_id, FamilyPermission.CREATE_RECORDS
            )
            if data.currency is None:
                family = await self.db.get(Family, data.family_id)
                currency = family.currency if family is not None else "INR"
        goal = SavingsGoal(
            owner_user_id=user.id,
            scope=data.scope,
            family_id=data.family_id,
            name=data.name,
            target_amount=data.target_amount,
            target_date=data.target_date,
            currency=currency,
            icon=data.icon,
            color=data.color,
            status=GoalStatus.ACTIVE,
        )
        self.repo.add(goal)
        await self.db.flush()
        self._audit("savings_goal.created", user, goal, client, name=goal.name)
        await self.db.commit()
        await self.db.refresh(goal)
        return await self._out(user, goal, now)

    async def list_goals(
        self,
        user: User,
        family_id: uuid.UUID | None,
        status: GoalStatus | None = None,
        now: datetime | None = None,
    ) -> list[GoalOut]:
        if family_id is not None:
            await require_family_access(self.db, user, family_id)
        goals = await self.repo.list_for_scope(user.id, family_id, status)
        return await self._out_many(user, goals, now or utcnow())

    async def get_goal(
        self, user: User, goal_id: uuid.UUID, now: datetime | None = None
    ) -> GoalOut:
        return await self._out(user, await self._visible(user, goal_id), now or utcnow())

    async def update_goal(
        self,
        user: User,
        goal_id: uuid.UUID,
        data: GoalUpdate,
        client: ClientInfo,
        now: datetime | None = None,
    ) -> GoalOut:
        now = now or utcnow()
        goal = await self._visible(user, goal_id, for_update=True)
        await require_record_write(self.db, user.id, goal)
        changes = data.model_dump(exclude_unset=True)
        new_status = changes.pop("status", None)
        for field, value in changes.items():
            setattr(goal, field, value)
        if new_status == "archived" and goal.status is not GoalStatus.ARCHIVED:
            goal.status = GoalStatus.ARCHIVED
            changes["status"] = new_status
        elif new_status == "active" and goal.status is GoalStatus.ARCHIVED:
            goal.status = GoalStatus.ACTIVE
            goal.completed_at = None
            changes["status"] = new_status
        if changes:
            await self.db.flush()
            await self._settle(goal, now)
            self._audit("savings_goal.updated", user, goal, client, fields=sorted(changes))
            await self.db.commit()
            await self.db.refresh(goal)
        return await self._out(user, goal, now)

    async def delete_goal(self, user: User, goal_id: uuid.UUID, client: ClientInfo) -> None:
        goal = await self._visible(user, goal_id, for_update=True)
        await require_record_write(self.db, user.id, goal)
        goal.deleted_at = utcnow()
        self._audit("savings_goal.deleted", user, goal, client, name=goal.name)
        await self.db.commit()

    # ------------------------------------------------------------------ contributions

    async def add_contribution(
        self,
        user: User,
        goal_id: uuid.UUID,
        data: ContributionCreate,
        client: ClientInfo,
        now: datetime | None = None,
    ) -> ContributionCreatedOut:
        now = now or utcnow()
        goal = await self._visible(user, goal_id, for_update=True)  # serialises balance checks
        await self._require_contribute(user, goal)
        if goal.status is GoalStatus.ARCHIVED:
            raise ConflictError("This goal is archived", code="goal_archived")
        if data.contributed_at is not None and data.contributed_at > now + FUTURE_SKEW:
            raise ValidationFailedError("contributed_at cannot be in the future")
        if data.transaction_id is not None:
            linked = await self.repo.visible_transaction(user.id, data.transaction_id)
            if linked is None:
                raise NotFoundError("Transaction not found")
            # Only a transaction of the goal's own scope (and currency) may be linked, so a
            # family goal never exposes a member's personal (or another family's) row.
            same_scope = linked.scope is goal.scope and (
                linked.family_id == goal.family_id
                if goal.scope is RecordScope.FAMILY
                else linked.owner_user_id == goal.owner_user_id
            )
            if not same_scope:
                raise ValidationFailedError(
                    "The transaction belongs to a different scope than the goal",
                    code="transaction_scope_mismatch",
                )
            if linked.currency != goal.currency:
                raise ValidationFailedError(
                    "The transaction's currency differs from the goal's",
                    code="currency_mismatch",
                )
        if data.kind is ContributionKind.WITHDRAWAL:
            balance = (await self.repo.balances([goal.id]))[goal.id]
            if data.amount > balance:
                raise ConflictError(
                    "A withdrawal cannot take the goal balance below zero",
                    code="insufficient_goal_balance",
                    details={"balance": f"{balance:f}"},
                )
        contribution = SavingsContribution(
            goal_id=goal.id,
            user_id=user.id,
            kind=data.kind,
            amount=data.amount,
            note=data.note,
            contributed_at=data.contributed_at or now,
            transaction_id=data.transaction_id,
        )
        self.repo.add(contribution)
        await self.db.flush()
        await self._settle(goal, now)
        self._audit(
            "savings_contribution.created",
            user,
            goal,
            client,
            entity_type="savings_contribution",
            entity_id=contribution.id,
            goal_id=str(goal.id),
            kind=data.kind.value,
            amount=f"{data.amount:f}",
        )
        await self.db.commit()
        await self.db.refresh(contribution)
        await self.db.refresh(goal)
        (contribution_out,) = await self._contributions_out(user, goal, [contribution])
        return ContributionCreatedOut(
            contribution=contribution_out, goal=await self._out(user, goal, now)
        )

    async def list_contributions(
        self, user: User, goal_id: uuid.UUID, page: PageParams
    ) -> Page[ContributionOut]:
        goal = await self._visible(user, goal_id)
        rows, total = await paginate(self.db, self.repo.contributions_stmt(goal.id), page)
        return Page[ContributionOut].build(
            await self._contributions_out(user, goal, list(rows)), total, page
        )

    async def delete_contribution(
        self,
        user: User,
        goal_id: uuid.UUID,
        contribution_id: uuid.UUID,
        client: ClientInfo,
        now: datetime | None = None,
    ) -> GoalOut:
        now = now or utcnow()
        goal = await self._visible(user, goal_id, for_update=True)
        contribution = await self.repo.get_contribution(goal.id, contribution_id)
        if contribution is None:
            raise NotFoundError("Contribution not found")
        if goal.scope is RecordScope.FAMILY:
            if goal.family_id is None:  # pragma: no cover - enforced by ck_*_scope_family
                raise NotFoundError()
            member = await get_active_membership(self.db, user, goal.family_id)
            if member is None:  # pragma: no cover - goal visibility already checked
                raise NotFoundError("Savings goal not found")
            allowed = role_has(member.role, FamilyPermission.MANAGE_ALL_RECORDS) or (
                role_has(member.role, FamilyPermission.CREATE_RECORDS)
                and contribution.user_id == user.id
            )
            if not allowed:
                raise ForbiddenError(
                    "You can only delete contributions you added",
                    code="insufficient_family_role",
                )
        if contribution.kind is ContributionKind.DEPOSIT:
            balance = (await self.repo.balances([goal.id]))[goal.id]
            if balance - contribution.amount < 0:
                raise ConflictError(
                    "Deleting this deposit would take the goal balance below zero",
                    code="insufficient_goal_balance",
                    details={"balance": f"{balance:f}"},
                )
        contribution.deleted_at = now
        await self.db.flush()
        await self._settle(goal, now)
        self._audit(
            "savings_contribution.deleted",
            user,
            goal,
            client,
            entity_type="savings_contribution",
            entity_id=contribution.id,
            goal_id=str(goal.id),
            kind=contribution.kind.value,
            amount=f"{contribution.amount:f}",
        )
        await self.db.commit()
        await self.db.refresh(goal)
        return await self._out(user, goal, now)

    # ------------------------------------------------------------------ history

    async def history(
        self, user: User, goal_id: uuid.UUID, now: datetime | None = None
    ) -> GoalHistoryOut:
        """Cumulative balance per month (goal owner's timezone), from the first month with a
        contribution through the current month, gaps filled."""
        now = now or utcnow()
        goal = await self._visible(user, goal_id)
        tz_name = (await self.repo.user_timezones([goal.owner_user_id])).get(goal.owner_user_id)
        tz = zone(tz_name)
        rows = await self.repo.monthly_totals(goal.id, tz.key)
        if not rows:
            return GoalHistoryOut(goal_id=goal.id, items=[])
        by_month = {m: (d, w) for m, d, w in rows}
        last = max(rows[-1][0], _month_key(now.astimezone(tz).date()))
        items: list[GoalHistoryPoint] = []
        balance = ZERO
        month = rows[0][0]
        while True:
            d, w = by_month.get(month, (ZERO, ZERO))
            balance += d - w
            items.append(
                GoalHistoryPoint(month=month, deposits=d, withdrawals=w, net=d - w, balance=balance)
            )
            if month >= last:
                break
            month = _next_month(month)
        return GoalHistoryOut(goal_id=goal.id, items=items)
