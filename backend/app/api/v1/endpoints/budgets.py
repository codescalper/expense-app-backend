"""Budget endpoints (/api/v1/budgets).

List-style routes take an optional ``family_id`` query parameter: absent → the caller's
personal budgets; present → that family's budgets (active membership required, else 404).
Family budgets can be created/edited/deleted by the family owner and admins only
(``MANAGE_BUDGETS``); members and viewers can read them. Budgets outside the caller's
visibility are 404.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import Client, CurrentPrincipal, DbSession
from app.schemas.budgets import (
    BudgetCreate,
    BudgetDetailOut,
    BudgetHistoryOut,
    BudgetOut,
    BudgetOverviewOut,
    BudgetUpdate,
)
from app.services.budgets import MAX_HISTORY_PERIODS, BudgetService

router = APIRouter(prefix="/budgets", tags=["budgets"])


def get_budget_service(db: DbSession) -> BudgetService:
    return BudgetService(db)


BudgetSvc = Annotated[BudgetService, Depends(get_budget_service)]
FamilyQuery = Annotated[
    uuid.UUID | None, Query(description="Omit for personal budgets; a family id for that family")
]


@router.get("/overview")
async def budget_overview(
    principal: CurrentPrincipal, svc: BudgetSvc, family_id: FamilyQuery = None
) -> BudgetOverviewOut:
    """Dashboard: this month's total budget vs spent, and budgets at warning/exceeded."""
    return await svc.overview(principal.user, family_id)


@router.get("")
async def list_budgets(
    principal: CurrentPrincipal,
    svc: BudgetSvc,
    family_id: FamilyQuery = None,
    is_active: Annotated[bool | None, Query(description="Filter by active flag")] = None,
) -> list[BudgetOut]:
    """Budgets in the scope, each with its current-period status."""
    return await svc.list_budgets(principal.user, family_id, is_active=is_active)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_budget(
    body: BudgetCreate, principal: CurrentPrincipal, svc: BudgetSvc, client: Client
) -> BudgetDetailOut:
    return await svc.create(principal.user, body, client)


@router.get("/{budget_id}")
async def get_budget(
    budget_id: uuid.UUID, principal: CurrentPrincipal, svc: BudgetSvc
) -> BudgetDetailOut:
    """Budget with its current period status and per-category breakdown."""
    return await svc.get_budget(principal.user, budget_id)


@router.get("/{budget_id}/history")
async def budget_history(
    budget_id: uuid.UUID,
    principal: CurrentPrincipal,
    svc: BudgetSvc,
    periods: Annotated[int, Query(ge=1, le=MAX_HISTORY_PERIODS)] = 6,
) -> BudgetHistoryOut:
    """Limit / spent for the last ``periods`` periods (oldest first, current last)."""
    return await svc.history(principal.user, budget_id, periods)


@router.patch("/{budget_id}")
async def update_budget(
    budget_id: uuid.UUID,
    body: BudgetUpdate,
    principal: CurrentPrincipal,
    svc: BudgetSvc,
    client: Client,
) -> BudgetDetailOut:
    return await svc.update(principal.user, budget_id, body, client)


@router.delete("/{budget_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_budget(
    budget_id: uuid.UUID, principal: CurrentPrincipal, svc: BudgetSvc, client: Client
) -> None:
    await svc.delete(principal.user, budget_id, client)
