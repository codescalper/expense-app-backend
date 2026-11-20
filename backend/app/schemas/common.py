from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, StringConstraints


class RequestModel(BaseModel):
    """Base for request bodies: unknown fields are rejected, strings are stripped."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class MessageResponse(ResponseModel):
    message: str


def _lower(value: str) -> str:
    return value.lower()


NormalizedEmail = Annotated[EmailStr, AfterValidator(_lower)]
# Passwords are not stripped and policy is enforced in the service layer (ensure_password_policy).
PasswordInput = Annotated[
    str, StringConstraints(min_length=1, max_length=128, strip_whitespace=False)
]
EmailCode = Annotated[str, StringConstraints(pattern=r"^\d{6}$")]
DeviceName = Annotated[str, StringConstraints(max_length=100)]
