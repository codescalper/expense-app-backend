"""Accounts & payment methods (spec §7).

Accounts never hold credentials: there are deliberately no fields for bank logins, card
numbers, CVVs or UPI PINs, and unknown fields are rejected (``RequestModel``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Self

from pydantic import Field, PlainSerializer, StringConstraints, model_validator

from app.models.enums import AccountType, RecordScope
from app.schemas.common import RequestModel, ResponseModel

CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
AccountName = Annotated[str, StringConstraints(min_length=1, max_length=80)]
Institution = Annotated[str, StringConstraints(min_length=1, max_length=80)]

CENT = Decimal("0.01")


def money_str(value: Decimal) -> str:
    """Decimal -> plain string with exactly two decimals (``"1250.50"``)."""
    return f"{value.quantize(CENT):f}"


# Money in responses: always a string with two decimals.
MoneyOut = Annotated[Decimal, PlainSerializer(money_str, return_type=str)]
# Signed money input (opening balances may be negative, e.g. a credit card owing money).
SignedMoneyIn = Annotated[Decimal, Field(max_digits=14, decimal_places=2, allow_inf_nan=False)]


class ScopedCreate(RequestModel):
    """``scope=personal`` (default, no ``family_id``) or ``scope=family`` + ``family_id``."""

    scope: RecordScope = RecordScope.PERSONAL
    family_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _scope_family(self) -> Self:
        if self.scope is RecordScope.FAMILY and self.family_id is None:
            raise ValueError("family_id is required when scope is 'family'")
        if self.scope is RecordScope.PERSONAL and self.family_id is not None:
            raise ValueError("family_id must be omitted when scope is 'personal'")
        return self


# --- requests --------------------------------------------------------------------------


class AccountCreate(ScopedCreate):
    name: AccountName
    type: AccountType
    institution: Institution | None = None
    opening_balance: SignedMoneyIn = Decimal("0.00")
    currency: CurrencyCode = "INR"


class AccountUpdate(RequestModel):
    """Partial update. ``institution: null`` clears it; other fields can't be null."""

    name: AccountName | None = None
    type: AccountType | None = None
    institution: Institution | None = None
    opening_balance: SignedMoneyIn | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        for field in ("name", "type", "opening_balance", "is_active"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


# --- responses -------------------------------------------------------------------------


class AccountOut(ResponseModel):
    id: uuid.UUID
    name: str
    type: AccountType
    institution: str | None
    currency: str
    opening_balance: MoneyOut
    current_balance: MoneyOut
    scope: RecordScope
    family_id: uuid.UUID | None
    owner_user_id: uuid.UUID
    is_active: bool
    can_manage: bool
    created_at: datetime
    updated_at: datetime
