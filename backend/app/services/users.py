"""Profile, password, avatar, session management and account deletion."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import BadRequestError, NotFoundError, RateLimitedError
from app.core.rate_limit import RateLimiter
from app.core.security import (
    UNUSABLE_PASSWORD,
    ensure_password_policy,
    hash_password_async,
    utcnow,
    verify_password_async,
)
from app.core.threads import run_limited
from app.models.user import User, UserSession
from app.repositories.system import AuditRepository
from app.repositories.users import SessionRepository
from app.schemas.user import ChangePasswordRequest, SessionOut, UserUpdate
from app.services.auth import ClientInfo
from app.services.notifications import disable_tokens_for_sessions, disable_tokens_for_user
from app.services.storage import LocalFileStorage, process_avatar

# Image decoding is CPU- and memory-heavy (up to ~50 MB for a 16.7 MP RGB image).
IMAGE_WORKERS = 2

INVALID_PASSWORD = "Password is incorrect"  # noqa: S105 - error message, not a secret


class UserService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.db = session
        self.settings = settings
        self.sessions = SessionRepository(session)
        self.audit = AuditRepository(session)
        self.storage = LocalFileStorage(settings.upload_dir)
        # Shares the request's connection/transaction (see app.core.rate_limit).
        self.limiter = RateLimiter(session)

    def _audit(self, action: str, user: User, client: ClientInfo, **details: object) -> None:
        self.audit.record(
            action,
            actor_user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            ip_address=client.ip,
            user_agent=client.user_agent,
            details=dict(details),
        )

    async def _reauthenticate(self, user: User, password: str, client: ClientInfo) -> None:
        """Re-check the current password for sensitive actions, with per-user throttling.

        A wrong password is a 400 ``invalid_password`` (not 401: the access token is fine,
        and clients treat 401 as "refresh the session")."""
        s = self.settings
        lock_key = f"reauth_lock:{user.id}"
        fail_key = f"reauth_fail:{user.id}"
        if s.rate_limit_enabled:
            remaining = await self.limiter.lock_remaining(lock_key)
            if remaining:
                raise RateLimitedError(remaining, "Too many wrong passwords. Try again later.")
            await self.limiter.hit(
                f"reauth:{user.id}", s.reauth_rate_limit, s.reauth_window_seconds
            )
        if await verify_password_async(user.password_hash, password):
            return
        if s.rate_limit_enabled:
            failures = await self.limiter.sliding_increment(fail_key, s.reauth_window_seconds)
            if failures >= s.reauth_max_failures:
                await self.limiter.lock(lock_key, s.reauth_window_seconds)
                await self.limiter.reset(fail_key)
                self._audit("user.reauth_locked", user, client)
        self._audit("user.reauth_failed", user, client)
        await self.db.commit()  # persist the failure even though we raise
        raise BadRequestError(INVALID_PASSWORD, code="invalid_password")

    async def update_profile(self, user: User, data: UserUpdate, client: ClientInfo) -> User:
        changes = data.model_dump(exclude_unset=True, exclude_none=True)
        for field, value in changes.items():
            setattr(user, field, value)
        if changes:
            self._audit("user.profile_updated", user, client, fields=sorted(changes))
            await self.db.commit()
        return user

    async def change_password(
        self,
        user: User,
        current_session_id: uuid.UUID,
        data: ChangePasswordRequest,
        client: ClientInfo,
    ) -> None:
        await self._reauthenticate(user, data.current_password, client)
        ensure_password_policy(data.new_password, user.email)
        now = utcnow()
        user.password_hash = await hash_password_async(data.new_password)
        user.password_changed_at = now
        revoked = await self.sessions.revoke_all_for_user(
            user.id, "password_changed", now, except_session_id=current_session_id
        )
        await disable_tokens_for_user(self.db, user.id, except_session_id=current_session_id)
        self._audit("user.password_changed", user, client, other_sessions_revoked=revoked)
        await self.db.commit()

    # --- avatar ---------------------------------------------------------------------

    async def set_avatar(self, user: User, raw: bytes, client: ClientInfo) -> User:
        s = self.settings
        if s.rate_limit_enabled:
            await self.limiter.hit(
                f"avatar:{user.id}", s.avatar_upload_limit, s.avatar_upload_window_seconds
            )
        # Decoding runs in a worker thread (bounded), never on the event loop.
        image = await run_limited(
            "image", IMAGE_WORKERS, process_avatar, raw, self.settings.max_avatar_bytes
        )
        old_key = user.avatar_key
        user.avatar_key = self.storage.save("avatars", image, "jpg")
        self._audit("user.avatar_updated", user, client)
        await self.db.commit()
        if old_key:
            self.storage.delete(old_key)
        return user

    async def remove_avatar(self, user: User, client: ClientInfo) -> None:
        if user.avatar_key:
            old_key = user.avatar_key
            user.avatar_key = None
            self._audit("user.avatar_removed", user, client)
            await self.db.commit()
            self.storage.delete(old_key)

    def avatar_path(self, user: User) -> Path:
        path = self.storage.path_for(user.avatar_key) if user.avatar_key else None
        if path is None:
            raise NotFoundError("No avatar")
        return path

    # --- sessions -------------------------------------------------------------------

    async def list_sessions(self, user: User, current_session_id: uuid.UUID) -> list[SessionOut]:
        rows = await self.sessions.list_active_for_user(user.id, utcnow())
        return [SessionOut.from_session(s, current_session_id) for s in rows]

    async def revoke_session(self, user: User, session_id: uuid.UUID, client: ClientInfo) -> None:
        now = utcnow()
        # Scoped to this user: another user's session id is indistinguishable from "missing".
        target = await self.sessions.get_active_for_user(session_id, user.id, now)
        if target is None:
            raise NotFoundError("Session not found")
        await self.sessions.revoke(target, "revoked_by_user", now)
        await disable_tokens_for_sessions(self.db, [target.id], now)
        self._audit("user.session_revoked", user, client, session_id=str(session_id))
        await self.db.commit()

    # --- account deletion -----------------------------------------------------------

    async def delete_account(self, user: User, password: str, client: ClientInfo) -> None:
        """Soft-delete and anonymise. Financial records added in later phases keep their
        foreign keys (owned by a now-anonymous user) so shared family history stays intact."""
        # Imported here: the family service imports user-side modules (avoids an import cycle).
        from app.services.families import FamilyService

        await self._reauthenticate(user, password, client)
        families = FamilyService(self.db, self.settings)
        # e.g. the owner of a family that still has other members must transfer it first (409)
        await families.assert_user_can_delete_account(user.id)
        now = utcnow()
        await families.detach_user_for_account_deletion(user.id, now)
        avatar_key = user.avatar_key
        self._audit("user.account_deleted", user, client)
        user.email = f"deleted-{user.id}@deleted.invalid"
        user.full_name = "Deleted user"
        user.password_hash = UNUSABLE_PASSWORD
        user.avatar_key = None
        user.is_active = False
        user.deleted_at = now
        await self.sessions.revoke_all_for_user(user.id, "account_deleted", now)
        await disable_tokens_for_user(self.db, user.id)
        # Device details are personal data: drop them from every session of this user.
        await self.db.execute(
            update(UserSession)
            .where(UserSession.user_id == user.id)
            .values(device_name=None, user_agent=None, ip_address=None)
            .execution_options(synchronize_session=False)
        )
        await self.db.commit()
        if avatar_key:
            self.storage.delete(avatar_key)
