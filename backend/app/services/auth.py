"""Authentication flows: register, verify email, login, refresh rotation, logout, password reset.

Security properties:
* Responses never reveal whether an email is registered (register / forgot-password always
  return 202; login failures and lockouts are generic and keyed on the email, so a real and an
  unknown address behave identically; unknown emails burn the same Argon2 time; unknown
  addresses enqueue a no-op job so the DB work matches).
* Unverified accounts belong to nobody yet: re-registering one replaces its password and name
  and kills earlier codes, and codes are bound to the password they were issued for, so the
  person who proves control of the inbox ends up with the password *they* chose.
* Refresh tokens rotate on every use. Re-presenting an already-rotated token is treated as
  theft and revokes the whole session. Sessions also have an absolute lifetime.
* Brute force: per-IP endpoint limits, a decaying per-email failure counter with a lockout,
  per-code attempt limits and a 24 h cap on wrong codes per account and purpose.
* All rate-limit state is written through the request's own session/connection (see
  ``app.core.rate_limit``), and Argon2 runs in a bounded thread pool.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NoReturn

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import BadRequestError, ForbiddenError, RateLimitedError, UnauthorizedError
from app.core.rate_limit import RateLimiter, hashed_key
from app.core.security import (
    burn_password_check_async,
    constant_time_equals,
    create_access_token,
    ensure_password_policy,
    generate_email_code,
    generate_refresh_token,
    hash_email_code,
    hash_password_async,
    hash_token,
    password_needs_rehash,
    utcnow,
    verify_password_async,
)
from app.models.user import RefreshToken, TokenPurpose, User, UserSession, UserToken
from app.repositories.system import AuditRepository, JobRepository
from app.repositories.users import SessionRepository, UserRepository, UserTokenRepository
from app.schemas.auth import (
    AuthResponse,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenPair,
    VerifyEmailRequest,
)
from app.schemas.user import UserOut
from app.services.email import (
    NOOP_EMAIL_JOB,
    SEND_EMAIL_JOB,
    OutgoingEmail,
    account_exists_email,
    password_reset_email,
    verification_email,
)
from app.services.notifications import disable_tokens_for_sessions, disable_tokens_for_user

INVALID_CREDENTIALS = "Invalid email or password"
INVALID_CODE = "Invalid or expired code"
TOO_MANY_FAILURES = "Too many failed sign-in attempts. Please try again later."

CODE_FAILURE_SPAN_SECONDS = 24 * 3600
CODE_FAILURE_BUCKET_SECONDS = 3600


@dataclass(frozen=True)
class ClientInfo:
    ip: str | None
    user_agent: str | None


def session_expiry(created_at: datetime, now: datetime, settings: Settings) -> datetime:
    """Sliding refresh expiry, never beyond the session's absolute lifetime."""
    sliding = now + timedelta(days=settings.refresh_token_expire_days)
    absolute = created_at + timedelta(days=settings.session_absolute_lifetime_days)
    return min(sliding, absolute)


class AuthService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.db = session
        self.settings = settings
        self.users = UserRepository(session)
        self.sessions = SessionRepository(session)
        self.codes = UserTokenRepository(session)
        self.audit = AuditRepository(session)
        self.jobs = JobRepository(session)
        # Counters share the request's connection and transaction (no second pooled
        # connection per request); failure paths commit before raising.
        self.limiter = RateLimiter(session)

    # ------------------------------------------------------------------ registration

    async def register(self, data: RegisterRequest, client: ClientInfo) -> None:
        ensure_password_policy(data.password, data.email)
        existing = await self.users.get_by_email(data.email, for_update=True)
        if existing is None:
            user = User(
                email=data.email,
                password_hash=await hash_password_async(data.password),
                full_name=data.full_name,
            )
            self.users.add(user)
            try:
                await self.db.flush()
            except IntegrityError:
                # Concurrent registration of the same email: behave like "already exists".
                await self.db.rollback()
                return
            await self._send_code(user, TokenPurpose.EMAIL_VERIFY)
            self._audit("auth.register", user, client)
        elif existing.is_email_verified:
            await burn_password_check_async(data.password)  # keep timing like the other paths
            await self._queue_email_limited(
                existing.email,
                "exists",
                account_exists_email(existing.email, existing.full_name),
            )
        else:
            # Nobody has proven control of this inbox yet, so an earlier (possibly hostile)
            # registration must not keep its password: the latest registration replaces it,
            # and every earlier code dies (also cryptographically: codes bind the password).
            now = utcnow()
            existing.password_hash = await hash_password_async(data.password)
            existing.full_name = data.full_name
            existing.password_changed_at = now
            await self.codes.invalidate_active(existing.id, TokenPurpose.EMAIL_VERIFY, now)
            await self.codes.invalidate_active(existing.id, TokenPurpose.PASSWORD_RESET, now)
            await self.sessions.revoke_all_for_user(existing.id, "re_registered", now)
            await disable_tokens_for_user(self.db, existing.id)
            await self._send_code(existing, TokenPurpose.EMAIL_VERIFY)
            self._audit("auth.register_replaced_unverified", existing, client)
        await self.db.commit()

    async def resend_verification(self, email: str) -> None:
        user = await self.users.get_by_email(email)
        if user is not None and user.is_active and not user.is_email_verified:
            await self._send_code(user, TokenPurpose.EMAIL_VERIFY)
        else:
            await self._send_noop(email, TokenPurpose.EMAIL_VERIFY)
        await self.db.commit()

    async def verify_email(self, data: VerifyEmailRequest, client: ClientInfo) -> AuthResponse:
        user = await self.users.get_by_email(data.email, for_update=True)
        if user is None or not user.is_active or user.is_email_verified:
            if data.password is not None:
                await burn_password_check_async(data.password)
            raise BadRequestError(INVALID_CODE, code="invalid_code")
        await self._consume_code(user, TokenPurpose.EMAIL_VERIFY, data.code, data.password)
        user.email_verified_at = utcnow()
        self._audit("auth.email_verified", user, client)
        response = await self._start_session(user, client, data.device_name)
        await self.db.commit()
        return response

    # ------------------------------------------------------------------ login / tokens

    async def login(self, data: LoginRequest, client: ClientInfo) -> AuthResponse:
        s = self.settings
        # All throttling state is keyed on the (HMAC'd) email, never on the user row, so a
        # registered and an unregistered address produce exactly the same responses.
        fail_key = hashed_key("login_fail", data.email)
        lock_key = hashed_key("login_lock", data.email)
        if s.rate_limit_enabled:
            remaining = await self.limiter.lock_remaining(lock_key)
            if remaining:
                raise RateLimitedError(remaining, TOO_MANY_FAILURES)

        user = await self.users.get_by_email(data.email, for_update=True)
        if user is None or not user.is_active:
            await burn_password_check_async(data.password)
            await self._login_failed(fail_key, lock_key, None, client)

        if not await verify_password_async(user.password_hash, data.password):
            self._audit("auth.login_failed", user, client)
            await self._login_failed(fail_key, lock_key, user, client)

        if not user.is_email_verified:
            # Only reachable with the correct password, so this reveals nothing new.
            raise ForbiddenError(
                "Please verify your email address first.", code="email_not_verified"
            )

        user.failed_login_count = 0
        user.locked_until = None
        if password_needs_rehash(user.password_hash):
            user.password_hash = await hash_password_async(data.password)
        response = await self._start_session(user, client, data.device_name)
        self._audit("auth.login", user, client)
        if s.rate_limit_enabled:
            await self.limiter.reset(fail_key)
        await self.db.commit()
        return response

    async def _login_failed(
        self, fail_key: str, lock_key: str, user: User | None, client: ClientInfo
    ) -> NoReturn:
        s = self.settings
        if s.rate_limit_enabled:
            failures = await self.limiter.sliding_increment(
                fail_key, s.login_failure_window_seconds
            )
            if failures >= s.login_max_failures:
                await self.limiter.lock(lock_key, s.login_lockout_seconds)
                await self.limiter.reset(fail_key)
                if user is not None:
                    self._audit("auth.account_locked", user, client)
        await self.db.commit()  # persist the failure even though we raise
        raise UnauthorizedError(INVALID_CREDENTIALS, code="invalid_credentials")

    async def refresh(self, raw_token: str, client: ClientInfo) -> TokenPair:
        now = utcnow()
        token = await self.sessions.get_refresh_token_for_update(hash_token(raw_token))
        if token is None:
            raise UnauthorizedError("Invalid refresh token", code="invalid_refresh_token")
        session = await self.db.get(UserSession, token.session_id, with_for_update=True)
        if session is None:
            raise UnauthorizedError("Invalid refresh token", code="invalid_refresh_token")

        if token.rotated_at is not None:
            # A rotated token came back: someone else has (or had) it. Kill the session.
            await self.sessions.revoke(session, "refresh_token_reuse", now)
            await disable_tokens_for_sessions(self.db, [session.id], now)
            self.audit.record(
                "auth.refresh_token_reuse_detected",
                actor_user_id=session.user_id,
                entity_type="user_session",
                entity_id=session.id,
                ip_address=client.ip,
                user_agent=client.user_agent,
            )
            await self.db.commit()
            raise UnauthorizedError("Refresh token already used", code="refresh_token_reused")

        user = await self.users.get(session.user_id)
        absolute_end = session.created_at + timedelta(
            days=self.settings.session_absolute_lifetime_days
        )
        if (
            session.revoked_at is not None
            or session.expires_at <= now
            or absolute_end <= now
            or token.expires_at <= now
            or user is None
            or not user.is_active
        ):
            raise UnauthorizedError(
                "Session expired. Please sign in again.", code="session_expired"
            )

        token.rotated_at = now
        session.last_used_at = now
        session.expires_at = session_expiry(session.created_at, now, self.settings)
        session.ip_address = client.ip
        pair = self._issue_tokens(session)
        await self.db.commit()
        return pair

    async def logout(self, user: User, session_id: uuid.UUID, client: ClientInfo) -> None:
        now = utcnow()
        session = await self.sessions.get_active_for_user(session_id, user.id, now)
        if session is not None:
            await self.sessions.revoke(session, "logout", now)
            await disable_tokens_for_sessions(self.db, [session.id], now)
        self._audit("auth.logout", user, client)
        await self.db.commit()

    async def logout_all(self, user: User, client: ClientInfo) -> int:
        count = await self.sessions.revoke_all_for_user(user.id, "logout_all", utcnow())
        await disable_tokens_for_user(self.db, user.id)
        self._audit("auth.logout_all", user, client, {"sessions_revoked": count})
        await self.db.commit()
        return count

    # ------------------------------------------------------------------ password reset

    async def forgot_password(self, email: str) -> None:
        user = await self.users.get_by_email(email)
        if user is not None and user.is_active:
            await self._send_code(user, TokenPurpose.PASSWORD_RESET)
        else:
            await self._send_noop(email, TokenPurpose.PASSWORD_RESET)
        await self.db.commit()

    async def reset_password(self, data: ResetPasswordRequest, client: ClientInfo) -> None:
        ensure_password_policy(data.new_password, data.email)
        user = await self.users.get_by_email(data.email, for_update=True)
        if user is None or not user.is_active:
            raise BadRequestError(INVALID_CODE, code="invalid_code")
        await self._consume_code(user, TokenPurpose.PASSWORD_RESET, data.code)
        now = utcnow()
        user.password_hash = await hash_password_async(data.new_password)
        user.password_changed_at = now
        user.failed_login_count = 0
        user.locked_until = None
        if user.email_verified_at is None:
            user.email_verified_at = now  # the code proves control of the inbox
        await self.sessions.revoke_all_for_user(user.id, "password_reset", now)
        await disable_tokens_for_user(self.db, user.id)
        if self.settings.rate_limit_enabled:
            # Inbox control proven: lift any sign-in lockout for this address.
            await self.limiter.reset(
                hashed_key("login_fail", data.email), hashed_key("login_lock", data.email)
            )
        self._audit("auth.password_reset", user, client)
        await self.db.commit()

    # ------------------------------------------------------------------ helpers

    async def _start_session(
        self, user: User, client: ClientInfo, device_name: str | None
    ) -> AuthResponse:
        now = utcnow()
        session = UserSession(
            user_id=user.id,
            device_name=device_name,
            user_agent=(client.user_agent or "")[:400] or None,
            ip_address=client.ip,
            last_used_at=now,
            expires_at=session_expiry(now, now, self.settings),
        )
        self.sessions.add(session)
        await self.db.flush()
        return AuthResponse(user=UserOut.from_user(user), tokens=self._issue_tokens(session))

    def _issue_tokens(self, session: UserSession) -> TokenPair:
        now = utcnow()
        raw = generate_refresh_token()
        self.sessions.add_refresh_token(
            RefreshToken(
                session_id=session.id,
                token_hash=hash_token(raw),
                issued_at=now,
                expires_at=session.expires_at,
            )
        )
        access, access_expires = create_access_token(session.user_id, session.id, self.settings)
        return TokenPair(
            access_token=access,
            refresh_token=raw,
            access_token_expires_at=access_expires,
            refresh_token_expires_at=session.expires_at,
        )

    @staticmethod
    def _code_hash(user: User, purpose: TokenPurpose, code: str) -> str:
        # Bound to the current password hash: changing the password (re-registration of an
        # unverified account, password change) invalidates every outstanding code.
        return hash_email_code(
            code, user_id=user.id, purpose=purpose.value, binding=user.password_hash
        )

    async def _send_code(self, user: User, purpose: TokenPurpose) -> None:
        minutes = self.settings.email_code_expire_minutes
        code = generate_email_code()
        message: OutgoingEmail
        if purpose is TokenPurpose.EMAIL_VERIFY:
            message = verification_email(user.email, code, minutes)
        else:
            # Never echo a name chosen by whoever registered an unverified address.
            name = user.full_name if user.is_email_verified else None
            message = password_reset_email(user.email, name, code, minutes)
        if not await self._queue_email_limited(user.email, purpose.value, message):
            return  # over the per-address quota: silently skip (no enumeration signal)
        now = utcnow()
        await self.codes.invalidate_active(user.id, purpose, now)
        self.codes.add(
            UserToken(
                user_id=user.id,
                purpose=purpose,
                code_hash=self._code_hash(user, purpose, code),
                expires_at=now + timedelta(minutes=minutes),
            )
        )

    async def _send_noop(self, email: str, purpose: TokenPurpose) -> None:
        """The unknown/ineligible-address twin of ``_send_code``: same quota bump, one queued
        job and an UPDATE round trip, so response time doesn't reveal whether it exists."""
        if await self._queue_email_limited(email, purpose.value, None):
            await self.codes.invalidate_active(uuid.uuid4(), purpose, utcnow())

    async def _queue_email_limited(
        self, email: str, kind: str, message: OutgoingEmail | None
    ) -> bool:
        s = self.settings
        if s.rate_limit_enabled:
            count, _ = await self.limiter.increment(
                hashed_key(f"email:{kind}", email), s.email_send_window_seconds
            )
            if count > s.email_send_limit:
                return False
        # The payload holds a plaintext one-time code: the worker drops it once expired.
        expires_at = utcnow() + timedelta(minutes=s.email_code_expire_minutes)
        if message is None:
            await self.jobs.enqueue(NOOP_EMAIL_JOB, {"expires_at": expires_at.isoformat()})
        else:
            await self.jobs.enqueue(SEND_EMAIL_JOB, message.to_payload(expires_at))
        return True

    async def _consume_code(
        self, user: User, purpose: TokenPurpose, code: str, password: str | None = None
    ) -> None:
        s = self.settings
        now = utcnow()
        fail_key = hashed_key(f"code_fail:{purpose.value}", str(user.id))
        if (
            s.rate_limit_enabled
            and await self.limiter.count_since(fail_key, CODE_FAILURE_SPAN_SECONDS)
            >= s.email_code_daily_max_failures
        ):
            # Too many wrong codes for this account in 24 h, across re-issued codes: refuse
            # (generically) until the window passes, so slow guessing can't add up.
            raise BadRequestError(INVALID_CODE, code="invalid_code")
        token = await self.codes.get_latest_active_for_update(user.id, purpose, now)
        if token is None:
            raise BadRequestError(INVALID_CODE, code="invalid_code")
        ok = constant_time_equals(token.code_hash, self._code_hash(user, purpose, code))
        if ok and password is not None:
            # The client also proved which registration it made (see VerifyEmailRequest).
            ok = await verify_password_async(user.password_hash, password)
        if not ok:
            token.attempts += 1
            if token.attempts >= s.email_code_max_attempts:
                token.consumed_at = now  # burn the code; user must request a new one
            if s.rate_limit_enabled:
                await self.limiter.increment_bucketed(
                    fail_key, CODE_FAILURE_SPAN_SECONDS, CODE_FAILURE_BUCKET_SECONDS
                )
            await self.db.commit()  # persist the attempt even though we raise
            raise BadRequestError(INVALID_CODE, code="invalid_code")
        token.consumed_at = now

    def _audit(
        self, action: str, user: User, client: ClientInfo, details: dict[str, object] | None = None
    ) -> None:
        self.audit.record(
            action,
            actor_user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            ip_address=client.ip,
            user_agent=client.user_agent,
            details=dict(details or {}),
        )
