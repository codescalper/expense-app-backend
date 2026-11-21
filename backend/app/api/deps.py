"""Shared FastAPI dependencies: settings, DB session, client info, authenticated principal."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import BackgroundTasks, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import UnauthorizedError
from app.core.outbox import start_collecting, stop_collecting
from app.core.rate_limit import RateLimiter, client_ip
from app.core.security import decode_access_token, utcnow
from app.db.session import get_db
from app.models.user import User, UserSession
from app.services.auth import AuthService, ClientInfo
from app.services.users import UserService
from app.workers.inline import deliver_enqueued_jobs

DbSession = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]

_bearer = HTTPBearer(auto_error=False, description="Access token from /auth/login")


async def inline_job_delivery(background_tasks: BackgroundTasks, settings: AppSettings) -> None:
    """Router-wide dependency. With ``EMAIL_DELIVERY=inline`` it records the jobs this request
    enqueues and delivers the email ones in a background task after the response is sent
    (see ``app.workers.inline``). Error responses carry no background tasks, so nothing runs
    for a request that failed. Must be async: it sets a context variable for the request."""
    if settings.email_delivery != "inline":
        stop_collecting()
        return
    background_tasks.add_task(deliver_enqueued_jobs, start_collecting())


def get_client_info(request: Request) -> ClientInfo:
    return ClientInfo(ip=client_ip(request), user_agent=request.headers.get("user-agent"))


Client = Annotated[ClientInfo, Depends(get_client_info)]


@dataclass(frozen=True)
class Principal:
    user: User
    session_id: uuid.UUID


async def get_principal(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    """Validates the JWT *and* that its session is still live, so logout is immediate."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise UnauthorizedError()
    claims = decode_access_token(credentials.credentials)
    now = utcnow()
    oldest_allowed = now - timedelta(days=get_settings().session_absolute_lifetime_days)
    row = (
        await db.execute(
            select(User, UserSession)
            .join(UserSession, UserSession.user_id == User.id)
            .where(
                UserSession.id == claims.session_id,
                User.id == claims.user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
                UserSession.created_at > oldest_allowed,  # absolute session lifetime
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
    ).first()
    if row is None:
        raise UnauthorizedError("Session is no longer valid", code="session_revoked")
    return Principal(user=row[0], session_id=claims.session_id)


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]

UserRateLimitKind = Literal["export", "analytics", "device_register"]


def user_rate_limit(kind: UserRateLimitKind) -> Callable[..., Awaitable[None]]:
    """Route dependency: per-user fixed-window limit ``<KIND>_RATE_LIMIT`` requests per
    ``<KIND>_RATE_WINDOW_SECONDS`` (429 ``rate_limited`` with Retry-After beyond it).

    The hit is committed on the request's session right away (that session already holds a
    connection for the principal lookup), so it counts even if the request later fails."""

    async def dependency(principal: CurrentPrincipal, db: DbSession, settings: AppSettings) -> None:
        if not settings.rate_limit_enabled:
            return
        limit = int(getattr(settings, f"{kind}_rate_limit"))
        window = int(getattr(settings, f"{kind}_rate_window_seconds"))
        await RateLimiter(db).hit(f"{kind}:{principal.user.id}", limit, window)
        await db.commit()

    return dependency


def get_auth_service(db: DbSession, settings: AppSettings) -> AuthService:
    return AuthService(db, settings)


def get_user_service(db: DbSession, settings: AppSettings) -> UserService:
    return UserService(db, settings)


AuthSvc = Annotated[AuthService, Depends(get_auth_service)]
UserSvc = Annotated[UserService, Depends(get_user_service)]
