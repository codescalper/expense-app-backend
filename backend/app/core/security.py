"""Password hashing (Argon2id), access JWTs, opaque refresh tokens and one-time email codes."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings, get_settings
from app.core.errors import UnauthorizedError, ValidationFailedError
from app.core.threads import run_limited

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"  # noqa: S105 - token type label, not a secret


def utcnow() -> datetime:
    return datetime.now(UTC)


# --- Passwords -------------------------------------------------------------------------


@lru_cache
def _hasher() -> PasswordHasher:
    s = get_settings()
    return PasswordHasher(
        time_cost=s.argon2_time_cost,
        memory_cost=s.argon2_memory_kib,
        parallelism=s.argon2_parallelism,
        type=Type.ID,
    )


def hash_password(password: str) -> str:
    return _hasher().hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher().verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher().check_needs_rehash(password_hash)
    except InvalidHashError:
        return False


@lru_cache
def _dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))


def burn_password_check(password: str) -> None:
    """Spend the same time as a real verification (prevents user-enumeration by timing)."""
    verify_password(_dummy_hash(), password)


# Async wrappers: Argon2 takes ~100 ms of CPU and 64 MiB per call, so it runs in a worker thread
# (never blocking the event loop) behind a small per-process limit (bounded memory and CPU).
_ARGON2 = "argon2"


def _argon2_capacity() -> int:
    return get_settings().password_hash_concurrency


async def hash_password_async(password: str) -> str:
    return await run_limited(_ARGON2, _argon2_capacity(), hash_password, password)


async def verify_password_async(password_hash: str, password: str) -> bool:
    return await run_limited(_ARGON2, _argon2_capacity(), verify_password, password_hash, password)


async def burn_password_check_async(password: str) -> None:
    await run_limited(_ARGON2, _argon2_capacity(), burn_password_check, password)


UNUSABLE_PASSWORD = "!"  # noqa: S105 - marker stored for deleted accounts; never verifies

_COMMON_PASSWORDS = frozenset(
    {
        "password", "password1", "password12", "password123", "password1234",
        "1234567890", "12345678910", "0987654321", "qwertyuiop", "qwerty12345",
        "iloveyou123", "letmein123", "welcome123", "admin12345", "abc1234567",
        "1q2w3e4r5t", "qwerty123456", "1111111111", "0000000000", "passw0rd123",
        "india12345", "indian1234", "familybudget", "budget1234",
    }
)  # fmt: skip

PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128


def ensure_password_policy(password: str, email: str | None = None) -> None:
    """NIST 800-63B style: length + blocklist, no arbitrary composition rules."""
    problems: list[str] = []
    if len(password) < PASSWORD_MIN_LENGTH:
        problems.append(f"Password must be at least {PASSWORD_MIN_LENGTH} characters.")
    if len(password) > PASSWORD_MAX_LENGTH:
        problems.append(f"Password must be at most {PASSWORD_MAX_LENGTH} characters.")
    lowered = password.lower()
    if lowered in _COMMON_PASSWORDS or len(set(password)) < 4:
        problems.append("Password is too common or too simple.")
    if email:
        local = email.split("@", 1)[0].lower()
        if lowered == email.lower() or (len(local) >= 4 and local in lowered):
            problems.append("Password must not contain your email address.")
    if problems:
        raise ValidationFailedError(
            "Password does not meet requirements",
            code="weak_password",
            details=[{"field": "password", "message": p} for p in problems],
        )


# --- Access tokens (JWT) ---------------------------------------------------------------


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    session_id: uuid.UUID
    expires_at: datetime


def create_access_token(
    user_id: uuid.UUID, session_id: uuid.UUID, settings: Settings | None = None
) -> tuple[str, datetime]:
    s = settings or get_settings()
    now = utcnow()
    expires_at = now + timedelta(minutes=s.access_token_expire_minutes)
    payload = {
        "sub": str(user_id),
        "sid": str(session_id),
        "typ": ACCESS_TOKEN_TYPE,
        "iss": s.jwt_issuer,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": secrets.token_hex(8),
    }
    token = jwt.encode(payload, s.jwt_secret_key.get_secret_value(), algorithm=JWT_ALGORITHM)
    return token, expires_at


def decode_access_token(token: str, settings: Settings | None = None) -> AccessClaims:
    s = settings or get_settings()
    try:
        payload = jwt.decode(
            token,
            s.jwt_secret_key.get_secret_value(),
            algorithms=[JWT_ALGORITHM],  # pinned: never accept "none" or asymmetric confusion
            issuer=s.jwt_issuer,
            options={"require": ["sub", "sid", "exp", "iat", "typ"]},
        )
        if payload.get("typ") != ACCESS_TOKEN_TYPE:
            raise UnauthorizedError("Invalid token", code="invalid_token")
        return AccessClaims(
            user_id=uuid.UUID(payload["sub"]),
            session_id=uuid.UUID(payload["sid"]),
            expires_at=datetime.fromtimestamp(payload["exp"], UTC),
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("Access token expired", code="token_expired") from exc
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise UnauthorizedError("Invalid token", code="invalid_token") from exc


# --- Opaque tokens & one-time codes ----------------------------------------------------


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_email_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_email_code(code: str, *, user_id: uuid.UUID, purpose: str, binding: str = "") -> str:
    """HMAC with the server secret so a leaked DB can't be brute-forced offline.

    ``binding`` ties the code to account state at issue time (verification codes bind the
    password hash of the registration they were sent for), so the code stops working as soon
    as that state changes.
    """
    key = get_settings().jwt_secret_key.get_secret_value().encode()
    msg = f"{purpose}:{user_id}:{code}"
    if binding:
        msg += ":" + hashlib.sha256(binding.encode()).hexdigest()
    return hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()


def keyed_hash(value: str, *, purpose: str) -> str:
    """HMAC-SHA256 with a purpose-specific key derived from the server secret.

    Used for identifiers stored outside their table (e.g. rate-limit keys for emails) so a DB
    dump can't be matched against a list of known addresses.
    """
    secret = get_settings().jwt_secret_key.get_secret_value().encode()
    subkey = hmac.new(secret, f"keyed-hash:{purpose}".encode(), hashlib.sha256).digest()
    return hmac.new(subkey, value.encode(), hashlib.sha256).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
