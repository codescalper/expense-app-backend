"""Saved payees / merchants and category suggestions."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, StringConstraints, model_validator

from app.models.enums import RecordScope
from app.schemas.categories import CategoryBriefOut, check_scope_family
from app.schemas.common import RequestModel, ResponseModel

VPA_RE = re.compile(r"^[a-z0-9._-]{2,256}@[a-z][a-z0-9]{1,63}$")
VPA_MAX_LENGTH = 255  # payees.upi_id column
_MOBILE_SEPARATORS = re.compile(r"[\s()-]")
_MOBILE_RE = re.compile(r"^(?:\+?91)?([6-9]\d{9})$")


def normalize_vpa(value: str) -> str:
    """Lower-cased UPI ID (VPA, ``name@handle``); ``ValueError`` if it is not one."""
    vpa = value.strip().lower()
    if len(vpa) > VPA_MAX_LENGTH or not VPA_RE.match(vpa):
        raise ValueError("Invalid UPI ID (expected something like name@bank)")
    return vpa


def normalize_mobile(value: str) -> str:
    """A 10-digit Indian mobile number (``+91``/``91`` prefix, spaces and dashes allowed)."""
    match = _MOBILE_RE.match(_MOBILE_SEPARATORS.sub("", value))
    if match is None:
        raise ValueError("Invalid mobile number (10-digit Indian number, optionally +91)")
    return match.group(1)


Vpa = Annotated[str, AfterValidator(normalize_vpa)]
Mobile = Annotated[str, StringConstraints(max_length=20), AfterValidator(normalize_mobile)]
PayeeName = Annotated[str, StringConstraints(min_length=1, max_length=120)]
MerchantCode = Annotated[str, StringConstraints(pattern=r"^\d{4}$")]  # UPI ``mc`` (MCC)


# --- requests --------------------------------------------------------------------------


class PayeeCreate(RequestModel):
    scope: RecordScope = RecordScope.PERSONAL
    family_id: uuid.UUID | None = None
    name: PayeeName
    upi_id: Vpa | None = None
    mobile: Mobile | None = None
    merchant_code: MerchantCode | None = None
    default_category_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _scope(self) -> PayeeCreate:
        check_scope_family(self.scope, self.family_id)
        return self


class PayeeUpdate(RequestModel):
    """Only the fields sent are changed; every field except ``name`` may be ``null``."""

    name: PayeeName | None = None
    upi_id: Vpa | None = None
    mobile: Mobile | None = None
    merchant_code: MerchantCode | None = None
    default_category_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _name_not_null(self) -> PayeeUpdate:
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be null")
        return self


# --- responses -------------------------------------------------------------------------


class PayeeOut(ResponseModel):
    id: uuid.UUID
    scope: RecordScope
    family_id: uuid.UUID | None
    owner_user_id: uuid.UUID
    name: str
    upi_id: str | None
    mobile: str | None
    merchant_code: str | None
    default_category_id: uuid.UUID | None
    use_count: int
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime


SuggestionSource = Literal["history", "payee_default", "keyword", "none"]


class CategorySuggestionOut(ResponseModel):
    category_id: uuid.UUID | None
    source: SuggestionSource
    confidence: float
    category: CategoryBriefOut | None
