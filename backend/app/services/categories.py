"""Transaction categories (built-in, personal, family) with one level of subcategories, and tag
autocomplete.

Rules:

* Built-in categories (migration 0002) are visible to everyone and read-only (403
  ``builtin_category``).
* Personal categories belong to their owner only; anyone else gets 404.
* Family categories are visible to active members; creating/changing them needs
  ``MANAGE_SHARED_SETUP`` (owner/admin), others get 403, non-members 404.
* A subcategory's parent must be a live, active, top-level category of the same kind usable in
  the same context (max depth 2). Names are unique case-insensitively among live siblings of the
  same kind in a context, built-ins included (409 ``category_name_taken``).
* Deleting is soft and takes the category's subcategories with it. If live transactions or
  recurring rules use any of them, ``reassign_to`` (a live, active category of the same kind in
  the same context) is required and everything is moved in the same DB transaction; otherwise
  409 ``category_in_use``. Budget category limits follow the same rule in that transaction:
  with ``reassign_to`` they are repointed to the target (merged — amounts added — into the
  budget's existing limit on the target, if any); without it they are removed. Budgets also
  ignore any limit row still pointing at a deleted category (see ``BudgetRepository``).
* ``GET /categories/{id}`` lists the children usable in the caller's personal context, or —
  for a built-in parent with ``family_id`` — in that family's context.
"""

from __future__ import annotations

import uuid
from collections import defaultdict

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationFailedError
from app.core.permissions import FamilyPermission, ScopeFilter, require_family_access
from app.core.security import utcnow
from app.models.enums import CategoryKind, RecordScope
from app.models.finance import TransactionCategory
from app.models.user import User
from app.repositories.categories import CategoryRepository, TagRepository
from app.repositories.system import AuditRepository
from app.schemas.categories import (
    CATEGORY_ICONS,
    CategoryCreate,
    CategoryDeleteOut,
    CategoryOut,
    CategoryUpdate,
    TagCountOut,
)
from app.services.auth import ClientInfo


class BuiltinCategoryError(ForbiddenError):
    code = "builtin_category"
    message = "Built-in categories cannot be changed"


class CategoryNameTakenError(ConflictError):
    code = "category_name_taken"
    message = "A category with this name already exists here"


def category_out(c: TransactionCategory, children: list[CategoryOut] | None = None) -> CategoryOut:
    ownership = "builtin" if c.is_builtin else ("family" if c.family_id else "personal")
    return CategoryOut(
        id=c.id,
        name=c.name,
        kind=c.kind,
        ownership=ownership,
        family_id=c.family_id,
        parent_id=c.parent_id,
        icon=c.icon,
        color=c.color,
        sort_order=c.sort_order,
        is_builtin=c.is_builtin,
        is_archived=c.is_archived,
        created_at=c.created_at,
        updated_at=c.updated_at,
        children=children or [],
    )


def build_tree(rows: list[TransactionCategory]) -> list[CategoryOut]:
    """Top-level categories with their children (rows must already be ordered). Children whose
    parent is not in ``rows`` (e.g. an archived parent) are left out."""
    children: defaultdict[uuid.UUID, list[CategoryOut]] = defaultdict(list)
    for row in rows:
        if row.parent_id is not None:
            children[row.parent_id].append(category_out(row))
    return [category_out(r, children.get(r.id, [])) for r in rows if r.parent_id is None]


class CategoryService:
    def __init__(self, session: AsyncSession) -> None:
        self.db = session
        self.categories = CategoryRepository(session)
        self.audit = AuditRepository(session)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def icons() -> list[str]:
        return list(CATEGORY_ICONS)

    def _audit(
        self,
        action: str,
        user: User,
        client: ClientInfo,
        category: TransactionCategory,
        **details: object,
    ) -> None:
        self.audit.record(
            action,
            actor_user_id=user.id,
            entity_type="category",
            entity_id=category.id,
            family_id=category.family_id,
            ip_address=client.ip,
            user_agent=client.user_agent,
            details=dict(details),
        )

    async def _load_for_write(self, user: User, category_id: uuid.UUID) -> TransactionCategory:
        """404 if not visible, 403 for built-ins or an insufficient family role."""
        category = await self.categories.get_live(category_id, for_update=True)
        if category is None:
            raise NotFoundError("Category not found")
        if category.is_builtin:
            raise BuiltinCategoryError()
        if category.family_id is not None:
            await require_family_access(
                self.db, user, category.family_id, FamilyPermission.MANAGE_SHARED_SETUP
            )
        elif category.owner_user_id != user.id:
            raise NotFoundError("Category not found")
        return category

    async def _valid_parent(
        self,
        user: User,
        family_id: uuid.UUID | None,
        parent_id: uuid.UUID,
        kind: CategoryKind,
    ) -> TransactionCategory:
        parent = await self.categories.get_in_context(user.id, family_id, parent_id)
        if parent is None or parent.kind != kind or parent.parent_id is not None:
            raise ValidationFailedError(
                "Parent must be an active top-level category of the same kind in the same scope",
                code="invalid_parent",
            )
        return parent

    async def _ensure_name_free(
        self,
        user: User,
        family_id: uuid.UUID | None,
        *,
        parent_id: uuid.UUID | None,
        kind: CategoryKind,
        name: str,
        exclude_id: uuid.UUID | None = None,
    ) -> None:
        if await self.categories.sibling_name_taken(
            user.id, family_id, parent_id=parent_id, kind=kind, name=name, exclude_id=exclude_id
        ):
            raise CategoryNameTakenError()

    async def _commit_unique(self) -> None:
        try:
            await self.db.commit()
        except IntegrityError as exc:  # concurrent insert of the same sibling name
            await self.db.rollback()
            raise CategoryNameTakenError() from exc

    async def _out_with_children(self, user: User, category: TransactionCategory) -> CategoryOut:
        children: list[CategoryOut] = []
        if category.parent_id is None:
            rows = await self.categories.list_in_context(
                user.id, category.family_id, parent_id=category.id, include_archived=True
            )
            children = [category_out(r) for r in rows]
        return category_out(category, children)

    # ------------------------------------------------------------------ reads

    async def list_tree(
        self,
        user: User,
        family_id: uuid.UUID | None,
        *,
        kind: CategoryKind | None = None,
        include_archived: bool = False,
    ) -> list[CategoryOut]:
        if family_id is not None:
            await require_family_access(self.db, user, family_id)
        rows = await self.categories.list_in_context(
            user.id, family_id, kind=kind, include_archived=include_archived
        )
        return build_tree(rows)

    async def get(
        self, user: User, category_id: uuid.UUID, family_id: uuid.UUID | None = None
    ) -> CategoryOut:
        """One category with its children. A built-in parent's children differ per context:
        the caller's personal ones by default, that family's with ``family_id`` (membership
        required, else 404). For other categories ``family_id`` is ignored — their children
        always come from their own context."""
        if family_id is not None:
            await require_family_access(self.db, user, family_id)
        category = await self.categories.get_visible(user.id, category_id)
        if category is None:
            raise NotFoundError("Category not found")
        if category.is_builtin:
            rows = await self.categories.list_in_context(
                user.id, family_id, parent_id=category.id, include_archived=True
            )
            return category_out(category, [category_out(r) for r in rows])
        return await self._out_with_children(user, category)

    # ------------------------------------------------------------------ writes

    async def create(self, user: User, data: CategoryCreate, client: ClientInfo) -> CategoryOut:
        family_id = data.family_id if data.scope is RecordScope.FAMILY else None
        if family_id is not None:
            await require_family_access(
                self.db, user, family_id, FamilyPermission.MANAGE_SHARED_SETUP
            )
        if data.parent_id is not None:
            await self._valid_parent(user, family_id, data.parent_id, data.kind)
        await self._ensure_name_free(
            user, family_id, parent_id=data.parent_id, kind=data.kind, name=data.name
        )
        category = TransactionCategory(
            owner_user_id=None if family_id else user.id,
            family_id=family_id,
            created_by_user_id=user.id,
            parent_id=data.parent_id,
            name=data.name,
            kind=data.kind,
            icon=data.icon,
            color=data.color,
            sort_order=data.sort_order,
        )
        self.categories.add(category)
        try:
            await self.db.flush()
        except IntegrityError as exc:
            await self.db.rollback()
            raise CategoryNameTakenError() from exc
        self._audit("category.created", user, client, category, name=data.name)
        await self._commit_unique()
        await self.db.refresh(category)
        return category_out(category)

    async def update(
        self, user: User, category_id: uuid.UUID, data: CategoryUpdate, client: ClientInfo
    ) -> CategoryOut:
        category = await self._load_for_write(user, category_id)
        fields = data.model_fields_set
        if not fields:
            return await self._out_with_children(user, category)
        family_id = category.family_id
        parent_id = data.parent_id if "parent_id" in fields else category.parent_id
        if "parent_id" in fields and data.parent_id is not None:
            if data.parent_id == category.id:
                raise ValidationFailedError(
                    "A category cannot be its own parent", code="invalid_parent"
                )
            await self._valid_parent(user, family_id, data.parent_id, category.kind)
            if await self.categories.live_child_ids(category.id):
                raise ValidationFailedError(
                    "A category with subcategories cannot become a subcategory",
                    code="invalid_parent",
                )
        name = data.name if data.name is not None else category.name
        if name.lower() != category.name.lower() or parent_id != category.parent_id:
            await self._ensure_name_free(
                user,
                family_id,
                parent_id=parent_id,
                kind=category.kind,
                name=name,
                exclude_id=category.id,
            )
        category.name = name
        category.parent_id = parent_id
        if "icon" in fields:
            category.icon = data.icon
        if "color" in fields:
            category.color = data.color
        if data.sort_order is not None:
            category.sort_order = data.sort_order
        self._audit("category.updated", user, client, category, fields=sorted(fields))
        await self._commit_unique()
        await self.db.refresh(category)
        return await self._out_with_children(user, category)

    async def set_archived(
        self, user: User, category_id: uuid.UUID, archived: bool, client: ClientInfo
    ) -> CategoryOut:
        category = await self._load_for_write(user, category_id)
        if category.is_archived != archived:
            category.is_archived = archived
            action = "category.archived" if archived else "category.unarchived"
            self._audit(action, user, client, category)
            await self.db.commit()
            await self.db.refresh(category)
        return await self._out_with_children(user, category)

    async def delete(
        self,
        user: User,
        category_id: uuid.UUID,
        reassign_to: uuid.UUID | None,
        client: ClientInfo,
    ) -> CategoryDeleteOut:
        category = await self._load_for_write(user, category_id)
        ids = [category.id, *await self.categories.live_child_ids(category.id)]
        target: TransactionCategory | None = None
        if reassign_to is not None:
            target = await self.categories.get_in_context(user.id, category.family_id, reassign_to)
            if target is None or target.kind != category.kind or target.id in ids:
                raise ValidationFailedError(
                    "reassign_to must be an active category of the same kind in the same scope "
                    "(not the deleted category or one of its subcategories)",
                    code="invalid_reassign_target",
                )
        tx_count = await self.categories.count_live_transactions(ids)
        recurring_count = await self.categories.count_live_recurring(ids)
        moved_tx = moved_recurring = moved_limits = removed_limits = 0
        if target is None:
            if tx_count or recurring_count:
                raise ConflictError(
                    "This category is in use; pass reassign_to to move its transactions",
                    code="category_in_use",
                    details={"transactions": tx_count, "recurring": recurring_count},
                )
            await self.categories.reassign_payee_defaults(ids, None)
            removed_limits = await self.categories.remove_budget_limits(ids)
        else:
            moved_tx = await self.categories.reassign_transactions(ids, target.id)
            moved_recurring = await self.categories.reassign_recurring(ids, target.id)
            await self.categories.reassign_payee_defaults(ids, target.id)
            moved_limits = await self.categories.move_budget_limits(ids, target.id)
        await self.categories.soft_delete(ids, utcnow())
        self._audit(
            "category.deleted",
            user,
            client,
            category,
            deleted_ids=[str(i) for i in ids],
            reassigned_to=str(target.id) if target else None,
            moved_transactions=moved_tx,
            moved_recurring=moved_recurring,
            moved_budget_limits=moved_limits,
            removed_budget_limits=removed_limits,
        )
        await self.db.commit()
        return CategoryDeleteOut(
            deleted_ids=ids,
            reassigned_to=target.id if target else None,
            moved_transactions=moved_tx,
            moved_recurring=moved_recurring,
            moved_budget_limits=moved_limits,
            removed_budget_limits=removed_limits,
        )


class TagService:
    def __init__(self, session: AsyncSession) -> None:
        self.db = session
        self.tags = TagRepository(session)

    async def list_counts(
        self, user: User, family_id: uuid.UUID | None, q: str | None, limit: int
    ) -> list[TagCountOut]:
        """Tags on live transactions in the personal context (own personal transactions) or in
        one family (membership required, else 404)."""
        if family_id is not None:
            await require_family_access(self.db, user, family_id)
            scope = ScopeFilter.for_user(user.id, family_id=family_id)
        else:
            scope = ScopeFilter.for_user(user.id, scope=RecordScope.PERSONAL)
        q = (q or "").strip() or None
        return [
            TagCountOut(name=n, count=c) for n, c in await self.tags.counts(scope, q=q, limit=limit)
        ]
