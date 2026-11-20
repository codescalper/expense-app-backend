"""Create in-app notifications (push delivery is done by the worker's push sender).

Producers (budgets, recurring bills, UPI reminders, families, savings) call ``notify`` /
``notify_family_members`` inside their own transaction; the caller commits. Every call should
pass a ``dedupe_key`` describing the event (e.g. ``budget:<id>:2026-11:75``) so retries and
periodic re-runs never create duplicates — the unique (user_id, dedupe_key) constraint
enforces it.

Preferences: missing row = both channels on. If a user turned off in-app *and* push for the
category, nothing is stored. If only push is off, the row is stored with push_status=skipped.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import NotificationCategory, PushStatus
from app.models.family import FamilyMember
from app.models.notification import Notification, NotificationPreference


async def _channels(
    session: AsyncSession, user_id: uuid.UUID, category: NotificationCategory
) -> tuple[bool, bool]:
    pref = await session.get(NotificationPreference, (user_id, category))
    if pref is None:
        return True, True
    return pref.in_app_enabled, pref.push_enabled


async def notify(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    category: NotificationCategory,
    title: str,
    body: str,
    dedupe_key: str | None,
    data: dict[str, Any] | None = None,
    family_id: uuid.UUID | None = None,
    scheduled_for: datetime | None = None,
) -> bool:
    """Store one notification. Returns True if a new row was created (False if it already
    existed for this dedupe_key or the user disabled the category entirely)."""
    in_app, push = await _channels(session, user_id, category)
    if not in_app and not push:
        return False
    values: dict[str, Any] = {
        "user_id": user_id,
        "family_id": family_id,
        "category": category,
        "title": title[:200],
        "body": body,
        "data": {**(data or {}), "category": category.value},
        "dedupe_key": dedupe_key,
        "push_status": PushStatus.PENDING if push else PushStatus.SKIPPED,
    }
    if scheduled_for is not None:
        values["scheduled_for"] = scheduled_for
    stmt = pg_insert(Notification).values(**values)
    if dedupe_key is not None:
        stmt = stmt.on_conflict_do_nothing(index_elements=["user_id", "dedupe_key"])
    result = await session.execute(stmt.returning(Notification.id))
    return result.scalar_one_or_none() is not None


async def notify_family_members(
    session: AsyncSession,
    *,
    family_id: uuid.UUID,
    category: NotificationCategory,
    title: str,
    body: str,
    dedupe_key: str,
    exclude_user_id: uuid.UUID | None = None,
    data: dict[str, Any] | None = None,
) -> int:
    """Notify every active member of a family (optionally excluding the actor). The dedupe key
    is per user, so the same key can be reused for all members. Returns rows created."""
    member_ids = list(
        await session.scalars(
            select(FamilyMember.user_id).where(
                FamilyMember.family_id == family_id, FamilyMember.left_at.is_(None)
            )
        )
    )
    created = 0
    for uid in member_ids:
        if uid == exclude_user_id:
            continue
        if await notify(
            session,
            user_id=uid,
            category=category,
            title=title,
            body=body,
            dedupe_key=dedupe_key,
            data=data,
            family_id=family_id,
        ):
            created += 1
    return created
