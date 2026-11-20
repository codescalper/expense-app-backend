from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import StringConstraints

from app.models.enums import DevicePlatform, NotificationCategory, PushStatus
from app.schemas.common import RequestModel, ResponseModel

# Expo push tokens: "ExponentPushToken[...]" (classic) or "ExpoPushToken[...]".
EXPO_TOKEN_PATTERN = r"^Expo(nent)?PushToken\[[A-Za-z0-9_\-]{1,200}\]$"  # noqa: S105 (a regex)
ExpoPushTokenStr = Annotated[str, StringConstraints(max_length=255, pattern=EXPO_TOKEN_PATTERN)]


# --- requests --------------------------------------------------------------------------


class PreferenceUpdate(RequestModel):
    category: NotificationCategory
    push_enabled: bool
    in_app_enabled: bool


class DeviceRegister(RequestModel):
    token: ExpoPushTokenStr
    platform: DevicePlatform


# --- responses -------------------------------------------------------------------------


class NotificationOut(ResponseModel):
    id: uuid.UUID
    category: NotificationCategory
    title: str
    body: str
    # Producer-supplied context (ids etc.) plus "category". Internal "_"-prefixed keys removed.
    data: dict[str, Any]
    # App deep link for this notification (same value as the push payload's "url").
    url: str
    family_id: uuid.UUID | None
    read: bool
    read_at: datetime | None
    created_at: datetime
    scheduled_for: datetime
    push_status: PushStatus


class UnreadCountOut(ResponseModel):
    count: int


class MarkedReadOut(ResponseModel):
    updated: int


class PreferenceOut(ResponseModel):
    category: NotificationCategory
    push_enabled: bool
    in_app_enabled: bool


class DeviceOut(ResponseModel):
    id: uuid.UUID
    platform: DevicePlatform
    # Never echo whole tokens back in lists; the client already knows its own token.
    token_hint: str
    created_at: datetime
    last_seen_at: datetime
