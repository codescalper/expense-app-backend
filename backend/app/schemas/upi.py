"""UPI intent payments (spec §6): request bodies and response shapes.

No request accepts payment credentials: any top-level field named like a PIN / MPIN /
password / CVV / OTP is rejected with 422 before anything else is validated (unknown fields are
rejected anyway by ``RequestModel``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, StringConstraints, model_validator

from app.models.enums import (
    RecordScope,
    TransactionStatus,
    UpiAttemptStatus,
    VerificationStatus,
)
from app.schemas.accounts import MoneyOut, ScopedCreate
from app.schemas.common import RequestModel, ResponseModel
from app.schemas.payees import CategorySuggestionOut, MerchantCode, PayeeOut
from app.schemas.transactions import AmountIn, PayeeRef, TransactionOut, UserRef
from app.services.upi_uri import (
    MAX_NAME_LENGTH,
    MAX_TEXT_LENGTH,
    UTR_PATTERN,
    UpiUriError,
    is_credential_key,
    normalize_payee_vpa,
)

# The transaction behind an attempt uses ``"upi:" + key`` (≤ 100 characters).
UPI_IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9._:\-]{1,96}$"
UpiIdempotencyKey = Annotated[str, StringConstraints(pattern=UPI_IDEMPOTENCY_KEY_PATTERN)]


def _vpa(value: str) -> str:
    try:
        return normalize_payee_vpa(value)
    except UpiUriError as exc:
        raise ValueError(exc.message) from exc


PayeeVpa = Annotated[str, StringConstraints(max_length=320), AfterValidator(_vpa)]
Utr = Annotated[
    str,
    StringConstraints(pattern=UTR_PATTERN, to_upper=True),
]

CREDENTIALS_MESSAGE = (
    "Payment credentials (UPI PIN, MPIN, passwords, CVV, OTP) are never accepted or stored"
)


class UpiRequest(RequestModel):
    """Base for UPI request bodies: rejects credential-like field names (422)."""

    @model_validator(mode="before")
    @classmethod
    def _no_credentials(cls, data: Any) -> Any:
        if isinstance(data, dict) and any(
            isinstance(k, str) and is_credential_key(k) for k in data
        ):
            raise ValueError(CREDENTIALS_MESSAGE)
        return data


# --- requests --------------------------------------------------------------------------


class UpiParseRequest(UpiRequest):
    """A scanned QR payload. ``family_id`` = the scope used for payee lookup / suggestions."""

    payload: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    family_id: uuid.UUID | None = None


class UpiInitiateRequest(UpiRequest, ScopedCreate):
    """Start a UPI payment. The idempotency key may also be sent as ``Idempotency-Key``."""

    payee_vpa: PayeeVpa
    payee_name: (
        Annotated[str, StringConstraints(min_length=1, max_length=MAX_NAME_LENGTH)] | None
    ) = None
    amount: AmountIn
    note: Annotated[str, StringConstraints(max_length=MAX_TEXT_LENGTH)] | None = None
    category_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    payee_id: uuid.UUID | None = None
    merchant_code: MerchantCode | None = None
    from_qr: bool = False
    qr_amount_locked: bool = False
    idempotency_key: UpiIdempotencyKey | None = None


class UpiReturnRequest(UpiRequest):
    """Whatever the UPI app returned (Android: the ``response`` string, e.g.
    ``txnId=…&responseCode=00&Status=SUCCESS&txnRef=…``). Untrusted; stored sanitised."""

    raw: Annotated[str, StringConstraints(max_length=8192)] | dict[str, Any] | None = None


ConfirmOutcome = Literal["success", "failure", "pending"]


class UpiConfirmRequest(UpiRequest):
    """What the *user* says happened. ``utr`` = the UPI reference / UTR shown by their app."""

    outcome: ConfirmOutcome
    utr: Utr | None = None


# --- responses -------------------------------------------------------------------------

STATUS_LABELS: dict[UpiAttemptStatus, str] = {
    UpiAttemptStatus.INITIATED: "Not paid yet",
    UpiAttemptStatus.LAUNCHED: "Awaiting your confirmation",
    UpiAttemptStatus.SUCCESS_REPORTED: "Paid (confirmed by user)",
    UpiAttemptStatus.FAILURE_REPORTED: "Failed (reported by user)",
    UpiAttemptStatus.PENDING_REPORTED: "Pending at bank",
    UpiAttemptStatus.CANCELLED: "Cancelled",
    UpiAttemptStatus.EXPIRED: "Expired",
}
VERIFICATION_LABELS: dict[VerificationStatus, str] = {
    VerificationStatus.UNVERIFIED: "Not verified",
    VerificationStatus.USER_CONFIRMED: "Confirmed by user, not verified with the bank",
    VerificationStatus.VERIFIED: "Verified",
}
OPEN_STATUSES = (
    UpiAttemptStatus.INITIATED,
    UpiAttemptStatus.LAUNCHED,
    UpiAttemptStatus.PENDING_REPORTED,
)


class UpiParseOut(ResponseModel):
    """Parsed QR fields (informational — the initiate call validates everything again)."""

    payee_vpa: str
    payee_name: str | None
    amount: MoneyOut | None
    amount_locked: bool
    currency: str
    note: str | None
    merchant_code: str | None
    reference: str | None
    mode: str | None
    purpose: str | None
    url: str | None
    unknown: dict[str, str]
    warnings: list[str]
    known_payee: PayeeOut | None
    suggested_category: CategorySuggestionOut


class UntrustedReturnHint(ResponseModel):
    """What the UPI app *claimed* on return. Never proof of payment: ask the user."""

    trusted: Literal[False] = False
    status: Literal["success", "failure", "submitted", "unknown"]
    txn_id: str | None
    response_code: str | None
    approval_ref: str | None
    txn_ref: str | None
    reference_matches: bool | None
    suggested_utr: str | None
    received_at: datetime | None
    message: str = "Reported by the UPI app and not verified. Ask the user what happened."


class UpiPaymentOut(ResponseModel):
    id: uuid.UUID
    scope: RecordScope
    family_id: uuid.UUID | None
    status: UpiAttemptStatus
    status_label: str
    verification: VerificationStatus
    verification_label: str
    needs_confirmation: bool
    payee_vpa: str
    payee_name: str | None
    payee_id: uuid.UUID | None
    amount: MoneyOut
    currency: str
    note: str | None
    tr_ref: str
    utr: str | None
    transaction_id: uuid.UUID | None
    transaction_status: TransactionStatus | None
    initiated_by: UserRef
    initiated_at: datetime
    launched_at: datetime | None
    confirmed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    can_edit: bool


class UpiPaymentDetailOut(UpiPaymentOut):
    """``upi_uri`` is only present while the payment can still be (re)launched
    (``initiated`` / ``launched``)."""

    upi_uri: str | None
    payee: PayeeRef | None
    transaction: TransactionOut | None
    untrusted_hint: UntrustedReturnHint | None
