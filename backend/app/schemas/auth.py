from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import StringConstraints

from app.schemas.common import (
    DeviceName,
    EmailCode,
    NormalizedEmail,
    PasswordInput,
    RequestModel,
    ResponseModel,
)
from app.schemas.user import FullName, UserOut


class RegisterRequest(RequestModel):
    email: NormalizedEmail
    password: PasswordInput
    full_name: FullName


class LoginRequest(RequestModel):
    email: NormalizedEmail
    password: PasswordInput
    device_name: DeviceName | None = None


class VerifyEmailRequest(RequestModel):
    email: NormalizedEmail
    code: EmailCode
    device_name: DeviceName | None = None
    # Optional but recommended: the password chosen at registration. When given it must match
    # the account's current password, so a registration made by someone else in between (who
    # can't read the inbox) can never be the one that gets verified.
    password: PasswordInput | None = None


class EmailOnlyRequest(RequestModel):
    email: NormalizedEmail


class ResetPasswordRequest(RequestModel):
    email: NormalizedEmail
    code: EmailCode
    new_password: PasswordInput


class RefreshRequest(RequestModel):
    refresh_token: Annotated[str, StringConstraints(min_length=20, max_length=200)]


class TokenPair(ResponseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth token type, not a secret
    access_token_expires_at: datetime
    refresh_token_expires_at: datetime


class AuthResponse(ResponseModel):
    user: UserOut
    tokens: TokenPair
