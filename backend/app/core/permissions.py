"""Family roles, the permission matrix and tenant-isolation helpers.

Every service touching personal/family data goes through this module. The rules:

* **Visibility.** A *personal* row (``scope='personal'``) is visible only to its
  ``owner_user_id``. A *family* row (``scope='family'``) is visible to every **active** member
  (``family_members.left_at IS NULL``) of a **live** family (``families.deleted_at IS NULL``).
* **Not visible ⇒ 404, never 403**, so the existence of other people's data is not leaked.
  A caller who *can see* a family but whose role is insufficient gets **403**.
* **Roles** (highest first): ``owner`` > ``admin`` > ``member`` > ``viewer``.

  ===================  =====  =====  ======  ======
  permission           owner  admin  member  viewer
  ===================  =====  =====  ======  ======
  VIEW                   ✓      ✓      ✓       ✓
  CREATE_RECORDS         ✓      ✓      ✓
  MANAGE_ALL_RECORDS     ✓      ✓
  MANAGE_BUDGETS         ✓      ✓
  MANAGE_SHARED_SETUP    ✓      ✓
  UPDATE_FAMILY          ✓      ✓
  INVITE_MEMBERS         ✓      ✓
  MANAGE_MEMBERS         ✓      ✓
  TRANSFER_OWNERSHIP     ✓
  DELETE_FAMILY          ✓
  ===================  =====  =====  ======  ======

  Members may edit/delete only family records **they created** (``owner_user_id``); admins and
  the owner may edit any family record (see :func:`can_modify_record`).

Typical service code::

    member = await require_family_access(db, user, family_id, FamilyPermission.CREATE_RECORDS)

    scope = ScopeFilter.for_user(user.id, family_id=family_id)
    stmt = scope.apply(select(Transaction), Transaction)

    row = await db.get(Transaction, tx_id)
    await require_record_write(db, user.id, row)   # 404 if invisible, 403 if not allowed
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import ColumnElement, Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenError, NotFoundError
from app.models.enums import FamilyRole, RecordScope
from app.models.family import Family, FamilyMember
from app.models.finance import TransactionCategory
from app.models.mixins import ScopedOwnershipMixin
from app.models.user import User

# --- roles & permissions ---------------------------------------------------------------

ROLE_RANK: Mapping[FamilyRole, int] = {
    FamilyRole.OWNER: 40,
    FamilyRole.ADMIN: 30,
    FamilyRole.MEMBER: 20,
    FamilyRole.VIEWER: 10,
}


class FamilyPermission(enum.StrEnum):
    VIEW = "view"
    """Read the family, its members and every family-scope record."""
    CREATE_RECORDS = "create_records"
    """Add family transactions / contributions / UPI payments; edit or delete *own* ones."""
    MANAGE_ALL_RECORDS = "manage_all_records"
    """Edit or delete *any* family record, whoever created it."""
    MANAGE_BUDGETS = "manage_budgets"
    """Create/edit/delete family budgets (incl. per-member budgets)."""
    MANAGE_SHARED_SETUP = "manage_shared_setup"
    """Family accounts, categories, payees, recurring rules and savings goals."""
    UPDATE_FAMILY = "update_family"
    """Rename the family, change currency, set/remove the family image."""
    INVITE_MEMBERS = "invite_members"
    """Create, list and revoke invitations (never with role ``owner``)."""
    MANAGE_MEMBERS = "manage_members"
    """Change roles / remove members — never the owner (see :func:`can_manage_member`)."""
    TRANSFER_OWNERSHIP = "transfer_ownership"
    DELETE_FAMILY = "delete_family"


_P = FamilyPermission
_VIEWER = frozenset({_P.VIEW})
_MEMBER = _VIEWER | {_P.CREATE_RECORDS}
_ADMIN = _MEMBER | {
    _P.MANAGE_ALL_RECORDS,
    _P.MANAGE_BUDGETS,
    _P.MANAGE_SHARED_SETUP,
    _P.UPDATE_FAMILY,
    _P.INVITE_MEMBERS,
    _P.MANAGE_MEMBERS,
}
_OWNER = _ADMIN | {_P.TRANSFER_OWNERSHIP, _P.DELETE_FAMILY}

PERMISSIONS: Mapping[FamilyRole, frozenset[FamilyPermission]] = {
    FamilyRole.OWNER: _OWNER,
    FamilyRole.ADMIN: _ADMIN,
    FamilyRole.MEMBER: _MEMBER,
    FamilyRole.VIEWER: _VIEWER,
}


def role_has(role: FamilyRole, permission: FamilyPermission) -> bool:
    """True if ``role`` grants ``permission`` (the matrix above)."""
    return permission in PERMISSIONS[role]


def role_at_least(role: FamilyRole, minimum: FamilyRole) -> bool:
    """Hierarchy comparison, e.g. ``role_at_least(m.role, FamilyRole.ADMIN)``."""
    return ROLE_RANK[role] >= ROLE_RANK[minimum]


def can_manage_member(
    actor_role: FamilyRole, target_role: FamilyRole, new_role: FamilyRole | None = None
) -> bool:
    """May ``actor_role`` change the role of (or remove) a member holding ``target_role``?

    * Nobody can change or remove the **owner** here (ownership moves only via transfer).
    * Nobody can *grant* ``owner`` here.
    * Owner and admins manage admins, members and viewers.
    """
    if not role_has(actor_role, FamilyPermission.MANAGE_MEMBERS):
        return False
    if target_role is FamilyRole.OWNER or new_role is FamilyRole.OWNER:
        return False
    return ROLE_RANK[actor_role] >= ROLE_RANK[target_role]


# --- membership lookups ----------------------------------------------------------------


def _user_id(user: User | uuid.UUID) -> uuid.UUID:
    return user if isinstance(user, uuid.UUID) else user.id


def active_family_ids_subquery(user_id: uuid.UUID) -> Select[uuid.UUID]:
    """``SELECT family_id`` of every live family where ``user_id`` is an active member.

    Use inside ``IN (...)`` so visibility is evaluated in the same query (no stale lists)."""
    return (
        select(FamilyMember.family_id)
        .join(Family, Family.id == FamilyMember.family_id)
        .where(
            FamilyMember.user_id == user_id,
            FamilyMember.left_at.is_(None),
            Family.deleted_at.is_(None),
        )
    )


async def get_active_membership(
    session: AsyncSession,
    user: User | uuid.UUID,
    family_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> FamilyMember | None:
    """The caller's active membership in a live family, or ``None``."""
    stmt = (
        select(FamilyMember)
        .join(Family, Family.id == FamilyMember.family_id)
        .where(
            FamilyMember.family_id == family_id,
            FamilyMember.user_id == _user_id(user),
            FamilyMember.left_at.is_(None),
            Family.deleted_at.is_(None),
        )
    )
    if for_update:
        stmt = stmt.with_for_update(of=FamilyMember)
    return await session.scalar(stmt)


async def require_family_access(
    session: AsyncSession,
    user: User | uuid.UUID,
    family_id: uuid.UUID,
    permission: FamilyPermission = FamilyPermission.VIEW,
    *,
    for_update: bool = False,
) -> FamilyMember:
    """Authorise ``user`` for ``permission`` in ``family_id`` and return their membership.

    Raises ``NotFoundError`` (404, code ``not_found``) when the family doesn't exist, is
    deleted, or the user is not an active member — indistinguishable on purpose. Raises
    ``ForbiddenError`` (403) when the user is a member but their role lacks ``permission``.
    ``for_update`` row-locks the membership (use when the decision guards a write that a
    concurrent role change could invalidate).
    """
    member = await get_active_membership(session, user, family_id, for_update=for_update)
    if member is None:
        raise NotFoundError("Family not found")
    if not role_has(member.role, permission):
        raise ForbiddenError(
            "Your role in this family does not allow this action",
            code="insufficient_family_role",
        )
    return member


async def list_user_family_ids(session: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """IDs of the live families where ``user_id`` is an active member."""
    return list(await session.scalars(active_family_ids_subquery(user_id)))


async def list_user_family_roles(
    session: AsyncSession, user_id: uuid.UUID
) -> dict[uuid.UUID, FamilyRole]:
    """``{family_id: role}`` for the user's active memberships in live families."""
    rows = await session.execute(
        select(FamilyMember.family_id, FamilyMember.role)
        .join(Family, Family.id == FamilyMember.family_id)
        .where(
            FamilyMember.user_id == user_id,
            FamilyMember.left_at.is_(None),
            Family.deleted_at.is_(None),
        )
    )
    return {family_id: role for family_id, role in rows}


# --- row visibility (tenant isolation) -------------------------------------------------


@dataclass(frozen=True)
class ScopeFilter:
    """Builds the WHERE clause limiting a *scoped* model (anything with ``owner_user_id``,
    ``family_id`` and ``scope`` — i.e. :class:`~app.models.mixins.ScopedOwnershipMixin`)
    to rows ``user_id`` may see.

    * ``ScopeFilter.for_user(uid)`` — personal rows owned by ``uid`` **or** family rows of any
      family ``uid`` is an active member of (the "everything I can see" view).
    * ``ScopeFilter.for_user(uid, scope=RecordScope.PERSONAL)`` — only my personal rows.
    * ``ScopeFilter.for_user(uid, family_id=fid)`` — only family rows of ``fid`` (and only if
      ``uid`` is an active member; otherwise the clause matches nothing). Call
      :func:`require_family_access` first if you want a 404 instead of an empty list.

    The membership check is a correlated ``IN (subquery)``, so the clause is safe on its own —
    never replace it with client-supplied family ids.
    """

    user_id: uuid.UUID
    family_id: uuid.UUID | None = None
    scope: RecordScope | None = None

    @classmethod
    def for_user(
        cls,
        user_id: uuid.UUID,
        *,
        family_id: uuid.UUID | None = None,
        scope: RecordScope | None = None,
    ) -> ScopeFilter:
        if family_id is not None and scope is RecordScope.PERSONAL:
            raise ValueError("family_id cannot be combined with scope=personal")
        return cls(user_id=user_id, family_id=family_id, scope=scope)

    def clause(self, model: type[ScopedOwnershipMixin]) -> ColumnElement[bool]:
        """The boolean SQL expression for ``model`` (does not filter soft-deleted rows)."""
        personal = and_(
            model.scope == RecordScope.PERSONAL,
            model.owner_user_id == self.user_id,
        )
        member_of = active_family_ids_subquery(self.user_id)
        if self.family_id is not None:
            return and_(
                model.scope == RecordScope.FAMILY,
                model.family_id == self.family_id,
                model.family_id.in_(member_of),
            )
        family = and_(model.scope == RecordScope.FAMILY, model.family_id.in_(member_of))
        if self.scope is RecordScope.PERSONAL:
            return personal
        if self.scope is RecordScope.FAMILY:
            return family
        return or_(personal, family)

    def apply[*Ts](
        self, stmt: Select[*Ts], model: type[ScopedOwnershipMixin], *, include_deleted: bool = False
    ) -> Select[*Ts]:
        """``stmt.where(clause)``, plus ``deleted_at IS NULL`` when the model soft-deletes."""
        stmt = stmt.where(self.clause(model))
        deleted_at = getattr(model, "deleted_at", None)
        if deleted_at is not None and not include_deleted:
            stmt = stmt.where(deleted_at.is_(None))
        return stmt


def visible_categories_clause(
    user_id: uuid.UUID, *, family_id: uuid.UUID | None = None
) -> ColumnElement[bool]:
    """Categories ``user_id`` may use: built-ins + own personal ones + those of their families.

    With ``family_id``: built-ins + that family's categories (if a member) — the set to offer
    when entering a family transaction. Soft-deleted rows are **not** excluded here.
    """
    c = TransactionCategory
    builtin = and_(c.owner_user_id.is_(None), c.family_id.is_(None))
    member_of = active_family_ids_subquery(user_id)
    if family_id is not None:
        return or_(builtin, and_(c.family_id == family_id, c.family_id.in_(member_of)))
    return or_(builtin, c.owner_user_id == user_id, c.family_id.in_(member_of))


def can_modify_record(
    user_id: uuid.UUID, row: ScopedOwnershipMixin, membership: FamilyMember | None
) -> bool:
    """May ``user_id`` edit/delete ``row``?

    * personal row → only its owner;
    * family row → owner/admin of that family (any row), or a ``member`` who created it.
      Viewers never. ``membership`` must be the caller's *active* membership in
      ``row.family_id`` (``None`` if they have none).
    """
    if row.scope is RecordScope.PERSONAL:
        return row.owner_user_id == user_id
    if membership is None or membership.family_id != row.family_id or not membership.is_active:
        return False
    if role_has(membership.role, FamilyPermission.MANAGE_ALL_RECORDS):
        return True
    return (
        role_has(membership.role, FamilyPermission.CREATE_RECORDS) and row.owner_user_id == user_id
    )


async def require_record_write(
    session: AsyncSession, user_id: uuid.UUID, row: ScopedOwnershipMixin | None
) -> FamilyMember | None:
    """Guard an update/delete of a scoped row loaded by id.

    * ``row`` is ``None``, soft-deleted, or not visible to the user → ``NotFoundError`` (404).
    * visible but not editable (viewer, or member editing someone else's row) → 403.

    Returns the caller's membership for family rows (``None`` for personal rows).
    """
    if row is None or getattr(row, "deleted_at", None) is not None:
        raise NotFoundError()
    if row.scope is RecordScope.PERSONAL:
        if row.owner_user_id != user_id:
            raise NotFoundError()
        return None
    if row.family_id is None:  # pragma: no cover - prevented by ck_*_scope_family
        raise NotFoundError()
    member = await get_active_membership(session, user_id, row.family_id)
    if member is None:
        raise NotFoundError()
    if not can_modify_record(user_id, row, member):
        raise ForbiddenError(
            "You can only change family records you created", code="insufficient_family_role"
        )
    return member
