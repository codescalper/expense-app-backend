"""Accounts, categories, payees and transactions (Phases 4-5).

Money columns are ``NUMERIC(14,2)`` (``Decimal``); amounts are always positive and the
direction comes from ``type``. Account balances are *computed* from transactions, never stored.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, SoftDeleteMixin, TimestampMixin, UUIDPkMixin
from app.models.enums import (
    ACCOUNT_TYPE,
    CATEGORY_KIND,
    PAYMENT_METHOD,
    TRANSACTION_SOURCE,
    TRANSACTION_STATUS,
    TRANSACTION_TYPE,
    VERIFICATION_STATUS,
    AccountType,
    CategoryKind,
    PaymentMethod,
    TransactionSource,
    TransactionStatus,
    TransactionType,
    VerificationStatus,
)
from app.models.mixins import ScopedOwnershipMixin, scope_family_check


class Account(UUIDPkMixin, ScopedOwnershipMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Bank / cash / UPI / card / wallet account. Current balance = opening_balance ± the
    account's completed, non-deleted transactions (computed in queries)."""

    __tablename__ = "accounts"
    __table_args__ = (
        scope_family_check(),
        Index("ix_accounts_owner_user_id", "owner_user_id"),
        Index("ix_accounts_family_id", "family_id"),
    )

    name: Mapped[str] = mapped_column(String(80))
    type: Mapped[AccountType] = mapped_column(ACCOUNT_TYPE)
    institution: Mapped[str | None] = mapped_column(String(80))
    opening_balance: Mapped[Decimal] = mapped_column(default=Decimal("0"), server_default=text("0"))
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))


class TransactionCategory(UUIDPkMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Expense/income category; ``parent_id`` makes it a subcategory.

    Ownership (exactly one of):
    * built-in default — ``owner_user_id IS NULL AND family_id IS NULL`` (seeded by migration
      0002, read-only for everyone);
    * personal — ``owner_user_id`` set;
    * family — ``family_id`` set (``created_by_user_id`` records who made it).
    """

    __tablename__ = "transaction_categories"
    __table_args__ = (
        CheckConstraint("owner_user_id IS NULL OR family_id IS NULL", name="single_owner"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="parent_not_self"),
        # Case-insensitive unique names among live siblings of the same owner.
        Index(
            "uq_transaction_categories_owner_name",
            "owner_user_id",
            "family_id",
            "parent_id",
            "kind",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_transaction_categories_family_id", "family_id"),
        Index("ix_transaction_categories_parent_id", "parent_id"),
    )

    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    family_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("families.id"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("transaction_categories.id"))
    name: Mapped[str] = mapped_column(String(60))
    kind: Mapped[CategoryKind] = mapped_column(CATEGORY_KIND)
    icon: Mapped[str | None] = mapped_column(String(40))
    color: Mapped[str | None] = mapped_column(String(9))
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"))
    is_archived: Mapped[bool] = mapped_column(default=False, server_default=text("false"))

    @property
    def is_builtin(self) -> bool:
        return self.owner_user_id is None and self.family_id is None


class Payee(UUIDPkMixin, ScopedOwnershipMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Saved payee / merchant. ``upi_id`` is a lowercase VPA (``name@bank``)."""

    __tablename__ = "payees"
    __table_args__ = (
        scope_family_check(),
        CheckConstraint("upi_id IS NULL OR upi_id = lower(upi_id)", name="upi_id_lowercase"),
        CheckConstraint("use_count >= 0", name="use_count_non_negative"),
        Index(
            "uq_payees_owner_upi_id",
            "owner_user_id",
            "family_id",
            "upi_id",
            unique=True,
            postgresql_where=text("upi_id IS NOT NULL AND deleted_at IS NULL"),
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_payees_owner_last_used", "owner_user_id", "last_used_at"),
        Index("ix_payees_family_id", "family_id"),
    )

    name: Mapped[str] = mapped_column(String(120))
    upi_id: Mapped[str | None] = mapped_column(String(255))
    mobile: Mapped[str | None] = mapped_column(String(15))
    merchant_code: Mapped[str | None] = mapped_column(String(10))
    default_category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transaction_categories.id")
    )
    last_used_at: Mapped[datetime | None]
    use_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))


class Transaction(UUIDPkMixin, ScopedOwnershipMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A money movement. ``owner_user_id`` = the user who created it.

    * ``amount`` is always > 0; ``type`` gives the direction. Transfers move money between
      ``account_id`` → ``to_account_id`` and are never income/expense in reports.
    * ``(recurring_id, period_key)`` is unique so a recurring rule can never generate the same
      period twice (even if the generated row is later soft-deleted).
    * ``(owner_user_id, idempotency_key)`` is unique for client retries / duplicate callbacks.
    """

    __tablename__ = "transactions"
    __table_args__ = (
        scope_family_check(),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(
            "(type = 'transfer' AND account_id IS NOT NULL AND to_account_id IS NOT NULL "
            "AND to_account_id <> account_id) OR (type <> 'transfer' AND to_account_id IS NULL)",
            name="transfer_accounts",
        ),
        CheckConstraint("(recurring_id IS NULL) = (period_key IS NULL)", name="recurring_period"),
        CheckConstraint(
            "(type = 'adjustment') = (adjustment_sign IS NOT NULL) "
            "AND (adjustment_sign IS NULL OR adjustment_sign IN (-1, 1))",
            name="adjustment_sign",
        ),
        UniqueConstraint("recurring_id", "period_key"),
        UniqueConstraint("owner_user_id", "idempotency_key"),
        UniqueConstraint("upi_attempt_id"),
        Index("ix_transactions_family_occurred", "family_id", "occurred_at"),
        Index("ix_transactions_owner_occurred", "owner_user_id", "occurred_at"),
        Index("ix_transactions_account_id", "account_id"),
        Index(
            "ix_transactions_to_account_id",
            "to_account_id",
            postgresql_where=text("to_account_id IS NOT NULL"),
        ),
        Index("ix_transactions_category_id", "category_id"),
        Index("ix_transactions_payee_id", "payee_id"),
    )

    type: Mapped[TransactionType] = mapped_column(TRANSACTION_TYPE)
    status: Mapped[TransactionStatus] = mapped_column(
        TRANSACTION_STATUS,
        default=TransactionStatus.COMPLETED,
        server_default=TransactionStatus.COMPLETED.value,
    )
    source: Mapped[TransactionSource] = mapped_column(
        TRANSACTION_SOURCE,
        default=TransactionSource.MANUAL,
        server_default=TransactionSource.MANUAL.value,
    )
    amount: Mapped[Decimal]
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    to_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("transaction_categories.id"))
    payee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payees.id"))
    merchant: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    payment_method: Mapped[PaymentMethod | None] = mapped_column(PAYMENT_METHOD)
    occurred_at: Mapped[datetime]
    recurring_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("recurring_transactions.id"))
    # The occurrence's scheduled date in ISO format (e.g. "2026-11-20") for every frequency —
    # written only by the recurring generator (app/services/recurrence.py ``period_key``).
    period_key: Mapped[str | None] = mapped_column(String(20))
    # use_alter: upi_payment_attempts.transaction_id points back here (FK cycle).
    upi_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("upi_payment_attempts.id", use_alter=True)
    )
    external_ref: Mapped[str | None] = mapped_column(String(64))  # UTR / bank reference
    idempotency_key: Mapped[str | None] = mapped_column(String(100))
    verification: Mapped[VerificationStatus] = mapped_column(
        VERIFICATION_STATUS,
        default=VerificationStatus.UNVERIFIED,
        server_default=VerificationStatus.UNVERIFIED.value,
    )
    # Only for type=adjustment: +1 raises the account balance, -1 lowers it.
    adjustment_sign: Mapped[int | None] = mapped_column(SmallInteger)


class TransactionTag(CreatedAtMixin, Base):
    __tablename__ = "transaction_tags"
    __table_args__ = (
        CheckConstraint("name = lower(name)", name="name_lowercase"),
        Index("ix_transaction_tags_name", "name"),
    )

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(40), primary_key=True)


class TransactionAttachment(UUIDPkMixin, CreatedAtMixin, SoftDeleteMixin, Base):
    """Receipt image/file. ``storage_key`` is server-generated (see services/storage.py)."""

    __tablename__ = "transaction_attachments"
    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="size_positive"),
        Index("ix_transaction_attachments_transaction_id", "transaction_id"),
    )

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE")
    )
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    uploaded_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
