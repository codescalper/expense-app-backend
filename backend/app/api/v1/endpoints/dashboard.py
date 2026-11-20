"""Home dashboard endpoint (/api/v1/dashboard)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import CurrentPrincipal, DbSession, user_rate_limit
from app.api.v1.endpoints.reports import Params
from app.schemas.dashboard import DashboardOut
from app.services.dashboard import DashboardService

router = APIRouter(
    prefix="/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(user_rate_limit("analytics"))],
)


def get_dashboard_service(db: DbSession) -> DashboardService:
    return DashboardService(db)


DashboardSvc = Annotated[DashboardService, Depends(get_dashboard_service)]


@router.get("")
async def get_dashboard(
    principal: CurrentPrincipal, svc: DashboardSvc, params: Params
) -> DashboardOut:
    """Every Home-screen card for the scope and period (default ``this_month``)."""
    return await svc.get(principal.user, params)
