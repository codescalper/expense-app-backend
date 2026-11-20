"""UPI intent payment attempts (Phase 5).

Never stores UPI PINs, bank credentials or card data. ``raw_return`` is whatever the UPI app
handed back and is **untrusted**; ``status``/``verification`` record what the user reported, not
a bank confirmation.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin
from app.models.enums import (
    UPI_ATTEMPT_STATUS,
    VERIFICATION_STATUS,
    UpiAttemptStatus,
    VerificationStatus,
)
from app.models.mixins import ScopedOwnershipMixin, scope_family_check


class UpiPaymentAttempt(UUIDPkMixin, ScopedOwnershipMixin, TimestampMixin, Base):
    __tablename__ = "upi_payment_attempts"
    __table_args__ = (
        scope_family_check(),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("payee_vpa = lower(payee_vpa)", name="payee_vpa_lowercase"),
        UniqueConstraint("owner_user_id", "idempotency_key"),
        Index("ix_upi_payment_attempts_owner_initiated", "owner_user_id", "initiated_at"),
        Index("ix_upi_payment_attempts_family_id", "family_id"),
        # Reconciliation / reminder scan of attempts that never got a final answer.
        Index(
            "ix_upi_payment_attempts_open",
            "initiated_at",
            postgresql_where=text("status IN ('initiated', 'launched', 'pending_reported')"),
        ),
    )

    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id"), unique=True
    )
    payee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payees.id"))
    payee_vpa: Mapped[str] = mapped_column(String(255))
    payee_name: Mapped[str | None] = mapped_column(String(120))
    amount: Mapped[Decimal]
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")
    note: Mapped[str | None] = mapped_column(String(80))
    # Our reference placed in the UPI URI (``tr``); max 35 chars per the UPI spec.
    tr_ref: Mapped[str] = mapped_column(String(35), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(100))
    status: Mapped[UpiAttemptStatus] = mapped_column(
        UPI_ATTEMPT_STATUS,
        default=UpiAttemptStatus.INITIATED,
        server_default=UpiAttemptStatus.INITIATED.value,
    )
    verification: Mapped[VerificationStatus] = mapped_column(
        VERIFICATION_STATUS,
        default=VerificationStatus.UNVERIFIED,
        server_default=VerificationStatus.UNVERIFIED.value,
    )
    utr: Mapped[str | None] = mapped_column(String(64))
    raw_return: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    initiated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    launched_at: Mapped[datetime | None]
    confirmed_at: Mapped[datetime | None]
