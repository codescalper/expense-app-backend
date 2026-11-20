"""Savings goal endpoints (/api/v1/savings).

``GET /savings`` takes an optional ``family_id``: absent → personal goals; present → that
family's goals (active membership required, else 404). Goals outside the caller's visibility
are 404; visible-but-not-allowed actions are 403.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import Client, CurrentPrincipal, DbSession
from app.models.enums import GoalStatus
from app.schemas.savings import (
    ContributionCreate,
    ContributionCreatedOut,
    ContributionOut,
    GoalCreate,
    GoalHistoryOut,
    GoalOut,
    GoalUpdate,
)
from app.services.savings import SavingsService
from app.utils.pagination import Page, Pagination

router = APIRouter(prefix="/savings", tags=["savings"])


def get_savings_service(db: DbSession) -> SavingsService:
    return SavingsService(db)


SavingsSvc = Annotated[SavingsService, Depends(get_savings_service)]


@router.get("")
async def list_goals(
    principal: CurrentPrincipal,
    svc: SavingsSvc,
    family_id: Annotated[uuid.UUID | None, Query()] = None,
    goal_status: Annotated[GoalStatus | None, Query(alias="status")] = None,
) -> list[GoalOut]:
    return await svc.list_goals(principal.user, family_id, goal_status)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_goal(
    body: GoalCreate, principal: CurrentPrincipal, svc: SavingsSvc, client: Client
) -> GoalOut:
    return await svc.create_goal(principal.user, body, client)


@router.get("/{goal_id}")
async def get_goal(goal_id: uuid.UUID, principal: CurrentPrincipal, svc: SavingsSvc) -> GoalOut:
    return await svc.get_goal(principal.user, goal_id)


@router.patch("/{goal_id}")
async def update_goal(
    goal_id: uuid.UUID,
    body: GoalUpdate,
    principal: CurrentPrincipal,
    svc: SavingsSvc,
    client: Client,
) -> GoalOut:
    return await svc.update_goal(principal.user, goal_id, body, client)


@router.delete("/{goal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_goal(
    goal_id: uuid.UUID, principal: CurrentPrincipal, svc: SavingsSvc, client: Client
) -> None:
    await svc.delete_goal(principal.user, goal_id, client)


@router.get("/{goal_id}/history")
async def goal_history(
    goal_id: uuid.UUID, principal: CurrentPrincipal, svc: SavingsSvc
) -> GoalHistoryOut:
    """Cumulative balance by month."""
    return await svc.history(principal.user, goal_id)


@router.get("/{goal_id}/contributions")
async def list_contributions(
    goal_id: uuid.UUID, principal: CurrentPrincipal, svc: SavingsSvc, page: Pagination
) -> Page[ContributionOut]:
    return await svc.list_contributions(principal.user, goal_id, page)


@router.post("/{goal_id}/contributions", status_code=status.HTTP_201_CREATED)
async def add_contribution(
    goal_id: uuid.UUID,
    body: ContributionCreate,
    principal: CurrentPrincipal,
    svc: SavingsSvc,
    client: Client,
) -> ContributionCreatedOut:
    """Deposit or withdraw. A withdrawal larger than the balance is rejected with 409."""
    return await svc.add_contribution(principal.user, goal_id, body, client)


@router.delete("/{goal_id}/contributions/{contribution_id}")
async def delete_contribution(
    goal_id: uuid.UUID,
    contribution_id: uuid.UUID,
    principal: CurrentPrincipal,
    svc: SavingsSvc,
    client: Client,
) -> GoalOut:
    """Soft-delete a contribution; returns the updated goal."""
    return await svc.delete_contribution(principal.user, goal_id, contribution_id, client)
