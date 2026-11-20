"""Category endpoints (/api/v1/categories).

``family_id`` absent → personal context (built-ins + my categories); present → that family's
context (built-ins + the family's categories; membership required, else 404).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import Client, CurrentPrincipal, DbSession
from app.models.enums import CategoryKind
from app.schemas.categories import CategoryCreate, CategoryDeleteOut, CategoryOut, CategoryUpdate
from app.services.categories import CategoryService

router = APIRouter(prefix="/categories", tags=["categories"])


def get_category_service(db: DbSession) -> CategoryService:
    return CategoryService(db)


CategorySvc = Annotated[CategoryService, Depends(get_category_service)]


@router.get("")
async def list_categories(
    principal: CurrentPrincipal,
    svc: CategorySvc,
    family_id: uuid.UUID | None = None,
    kind: CategoryKind | None = None,
    include_archived: bool = False,
) -> list[CategoryOut]:
    """Tree of top-level categories, each with its ``children`` (one level)."""
    return await svc.list_tree(
        principal.user, family_id, kind=kind, include_archived=include_archived
    )


@router.get("/icons")
async def list_icons(principal: CurrentPrincipal) -> list[str]:
    """Allowed Lucide icon names for categories."""
    return CategoryService.icons()


@router.get("/{category_id}")
async def get_category(
    category_id: uuid.UUID,
    principal: CurrentPrincipal,
    svc: CategorySvc,
    family_id: Annotated[
        uuid.UUID | None,
        Query(description="For a built-in parent: list that family's subcategories"),
    ] = None,
) -> CategoryOut:
    """The category with its ``children`` (subcategories, archived ones included)."""
    return await svc.get(principal.user, category_id, family_id)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_category(
    body: CategoryCreate, principal: CurrentPrincipal, svc: CategorySvc, client: Client
) -> CategoryOut:
    """Personal, or family (owner/admin). ``parent_id`` makes it a subcategory."""
    return await svc.create(principal.user, body, client)


@router.patch("/{category_id}")
async def update_category(
    category_id: uuid.UUID,
    body: CategoryUpdate,
    principal: CurrentPrincipal,
    svc: CategorySvc,
    client: Client,
) -> CategoryOut:
    return await svc.update(principal.user, category_id, body, client)


@router.post("/{category_id}/archive")
async def archive_category(
    category_id: uuid.UUID, principal: CurrentPrincipal, svc: CategorySvc, client: Client
) -> CategoryOut:
    return await svc.set_archived(principal.user, category_id, True, client)


@router.post("/{category_id}/unarchive")
async def unarchive_category(
    category_id: uuid.UUID, principal: CurrentPrincipal, svc: CategorySvc, client: Client
) -> CategoryOut:
    return await svc.set_archived(principal.user, category_id, False, client)


@router.delete("/{category_id}")
async def delete_category(
    category_id: uuid.UUID,
    principal: CurrentPrincipal,
    svc: CategorySvc,
    client: Client,
    reassign_to: Annotated[
        uuid.UUID | None,
        Query(description="Category that receives this category's transactions (if any)"),
    ] = None,
) -> CategoryDeleteOut:
    """Soft-deletes the category and its subcategories. 409 ``category_in_use`` if transactions
    or recurring rules use them and ``reassign_to`` is not given."""
    return await svc.delete(principal.user, category_id, reassign_to, client)
