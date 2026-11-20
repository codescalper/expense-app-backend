"""Notification inbox, preferences and push device endpoints (/api/v1/notifications).

Users only ever see their own notifications and devices; anything else is 404.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Path, Query, status

from app.api.deps import AppSettings, CurrentPrincipal, DbSession, user_rate_limit
from app.models.enums import NotificationCategory
from app.schemas.notifications import (
    DeviceOut,
    DeviceRegister,
    MarkedReadOut,
    NotificationOut,
    PreferenceOut,
    PreferenceUpdate,
    UnreadCountOut,
)
from app.services.notifications import NotificationService
from app.utils.pagination import Page, Pagination

router = APIRouter(prefix="/notifications", tags=["notifications"])


def get_notification_service(db: DbSession, settings: AppSettings) -> NotificationService:
    return NotificationService(db, settings)


NotificationSvc = Annotated[NotificationService, Depends(get_notification_service)]
CategoryFilter = Annotated[NotificationCategory | None, Query(description="Only this category")]


# --- static routes first (before /{notification_id}) ------------------------------------


@router.get("")
async def list_notifications(
    principal: CurrentPrincipal,
    svc: NotificationSvc,
    page: Pagination,
    category: CategoryFilter = None,
    unread_only: Annotated[bool, Query(description="Only unread notifications")] = False,
) -> Page[NotificationOut]:
    """My notifications, newest first (only those whose scheduled time has passed)."""
    return await svc.list_notifications(
        principal.user, page, unread_only=unread_only, category=category
    )


@router.get("/unread-count")
async def unread_count(principal: CurrentPrincipal, svc: NotificationSvc) -> UnreadCountOut:
    return UnreadCountOut(count=await svc.unread_count(principal.user))


@router.post("/read-all")
async def mark_all_read(
    principal: CurrentPrincipal, svc: NotificationSvc, category: CategoryFilter = None
) -> MarkedReadOut:
    """Mark every (or every ``category``) unread notification as read."""
    return MarkedReadOut(updated=await svc.mark_all_read(principal.user, category))


@router.get("/preferences")
async def get_preferences(principal: CurrentPrincipal, svc: NotificationSvc) -> list[PreferenceOut]:
    """One entry per category; categories never changed report both channels enabled."""
    return await svc.get_preferences(principal.user)


@router.put("/preferences")
async def update_preferences(
    principal: CurrentPrincipal,
    svc: NotificationSvc,
    body: Annotated[list[PreferenceUpdate], Body(max_length=len(NotificationCategory))],
) -> list[PreferenceOut]:
    """Set the listed categories (others unchanged). Returns all categories."""
    return await svc.update_preferences(principal.user, body)


@router.post("/devices", dependencies=[Depends(user_rate_limit("device_register"))])
async def register_device(
    body: DeviceRegister, principal: CurrentPrincipal, svc: NotificationSvc
) -> DeviceOut:
    """Register (or re-bind) this device's Expo push token to the current session."""
    return await svc.register_device(principal.user, principal.session_id, body)


@router.delete("/devices/{token}", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_device(
    token: Annotated[str, Path(max_length=255)], principal: CurrentPrincipal, svc: NotificationSvc
) -> None:
    """Remove one of my push tokens (URL-encode the brackets). 404 if not mine."""
    await svc.unregister_device(principal.user, token)


# --- one notification -------------------------------------------------------------------


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: uuid.UUID, principal: CurrentPrincipal, svc: NotificationSvc
) -> NotificationOut:
    return await svc.mark_read(principal.user, notification_id)


@router.delete("/{notification_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notification(
    notification_id: uuid.UUID, principal: CurrentPrincipal, svc: NotificationSvc
) -> None:
    await svc.delete(principal.user, notification_id)
