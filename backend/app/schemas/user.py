from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime
from functools import lru_cache
from typing import Annotated
from zoneinfo import available_timezones

from pydantic import AfterValidator, StringConstraints

from app.models.user import User, UserSession
from app.schemas.common import PasswordInput, RequestModel, ResponseModel


@lru_cache(maxsize=1)
def _timezones() -> frozenset[str]:
    return frozenset(available_timezones())


def _valid_timezone(value: str) -> str:
    if value not in _timezones():
        raise ValueError("Unknown IANA time zone")
    return value


TimeZoneName = Annotated[str, StringConstraints(max_length=64), AfterValidator(_valid_timezone)]

# Bidirectional-text controls can make a name render differently from what it contains.
_BIDI_CONTROLS = frozenset("\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_WHITESPACE_RUN = re.compile(r"\s+")


def _clean_name(value: str) -> str:
    """Replace control characters (newlines, tabs, NUL, ...), line/paragraph separators and
    bidi overrides with spaces, then collapse whitespace. Joiners (ZWJ/ZWNJ), which Indic
    scripts and emoji need, are kept."""
    cleaned = "".join(
        " " if unicodedata.category(ch) in ("Cc", "Zl", "Zp") or ch in _BIDI_CONTROLS else ch
        for ch in value
    )
    cleaned = _WHITESPACE_RUN.sub(" ", cleaned).strip()
    if not cleaned:
        raise ValueError("Name must contain visible characters")
    return cleaned


FullName = Annotated[
    str, StringConstraints(min_length=1, max_length=120), AfterValidator(_clean_name)
]


class UserOut(ResponseModel):
    """Public view of the authenticated user. Never includes hashes or lockout state."""

    id: uuid.UUID
    email: str
    full_name: str
    timezone: str
    email_verified: bool
    has_avatar: bool
    created_at: datetime

    @classmethod
    def from_user(cls, user: User) -> UserOut:
        return cls(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            timezone=user.timezone,
            email_verified=user.is_email_verified,
            has_avatar=user.avatar_key is not None,
            created_at=user.created_at,
        )


class UserUpdate(RequestModel):
    full_name: FullName | None = None
    timezone: TimeZoneName | None = None


class ChangePasswordRequest(RequestModel):
    current_password: PasswordInput
    new_password: PasswordInput


class DeleteAccountRequest(RequestModel):
    password: PasswordInput


class SessionOut(ResponseModel):
    id: uuid.UUID
    device_name: str | None
    user_agent: str | None
    ip_address: str | None
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    is_current: bool

    @classmethod
    def from_session(cls, s: UserSession, current_session_id: uuid.UUID) -> SessionOut:
        return cls(
            id=s.id,
            device_name=s.device_name,
            user_agent=s.user_agent,
            ip_address=s.ip_address,
            created_at=s.created_at,
            last_used_at=s.last_used_at,
            expires_at=s.expires_at,
            is_current=s.id == current_session_id,
        )
