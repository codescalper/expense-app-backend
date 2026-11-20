from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import RefreshToken, TokenPurpose, User, UserSession, UserToken


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: uuid.UUID) -> User | None:
        return await self.session.scalar(
            select(User).where(User.id == user_id, User.deleted_at.is_(None))
        )

    async def get_by_email(self, email: str, *, for_update: bool = False) -> User | None:
        stmt = select(User).where(User.email == email.lower(), User.deleted_at.is_(None))
        if for_update:
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt)

    def add(self, user: User) -> None:
        self.session.add(user)


class SessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, user_session: UserSession) -> None:
        self.session.add(user_session)

    def add_refresh_token(self, token: RefreshToken) -> None:
        self.session.add(token)

    async def get_active_for_user(
        self, session_id: uuid.UUID, user_id: uuid.UUID, now: datetime
    ) -> UserSession | None:
        """The session must belong to *this* user (IDOR guard) and still be valid."""
        return await self.session.scalar(
            select(UserSession).where(
                UserSession.id == session_id,
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
            )
        )

    async def list_active_for_user(self, user_id: uuid.UUID, now: datetime) -> list[UserSession]:
        result = await self.session.scalars(
            select(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
            )
            .order_by(UserSession.last_used_at.desc())
        )
        return list(result)

    async def get_refresh_token_for_update(self, token_hash: str) -> RefreshToken | None:
        """Row-locks the token so concurrent refreshes with the same token serialize."""
        return await self.session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
        )

    async def revoke(self, user_session: UserSession, reason: str, now: datetime) -> None:
        if user_session.revoked_at is None:
            user_session.revoked_at = now
            user_session.revoked_reason = reason

    async def revoke_all_for_user(
        self,
        user_id: uuid.UUID,
        reason: str,
        now: datetime,
        *,
        except_session_id: uuid.UUID | None = None,
    ) -> int:
        stmt = (
            update(UserSession)
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=now, revoked_reason=reason)
            .execution_options(synchronize_session=False)
        )
        if except_session_id is not None:
            stmt = stmt.where(UserSession.id != except_session_id)
        result = await self.session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)


class UserTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def invalidate_active(
        self, user_id: uuid.UUID, purpose: TokenPurpose, now: datetime
    ) -> None:
        await self.session.execute(
            update(UserToken)
            .where(
                UserToken.user_id == user_id,
                UserToken.purpose == purpose,
                UserToken.consumed_at.is_(None),
            )
            .values(consumed_at=now)
            .execution_options(synchronize_session=False)
        )

    def add(self, token: UserToken) -> None:
        self.session.add(token)

    async def get_latest_active_for_update(
        self, user_id: uuid.UUID, purpose: TokenPurpose, now: datetime
    ) -> UserToken | None:
        return await self.session.scalar(
            select(UserToken)
            .where(
                UserToken.user_id == user_id,
                UserToken.purpose == purpose,
                UserToken.consumed_at.is_(None),
                UserToken.expires_at > now,
            )
            .order_by(UserToken.created_at.desc())
            .limit(1)
            .with_for_update()
        )
