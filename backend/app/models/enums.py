"""Domain enums shared by the Phase 3-7 tables, and their PostgreSQL enum types.

Each ``StrEnum`` maps to exactly one PostgreSQL enum type. Several tables share a type (for
example ``record_scope``), so the SQLAlchemy ``Enum`` objects are defined once here and reused
by every column. Values are stored as the lowercase strings below (``values_callable``).

Adding a value later needs a hand-written ``ALTER TYPE ... ADD VALUE`` migration (autogenerate
does not detect it) — see docs/MIGRATIONS.md.
"""

from __future__ import annotations

import enum

from sqlalchemy import Enum


def pg_enum(enum_cls: type[enum.StrEnum], name: str) -> Enum:
    """A named PostgreSQL enum storing the members' *values* (not their Python names)."""
    return Enum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e])


# --- families --------------------------------------------------------------------------


class FamilyRole(enum.StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class InvitationStatus(enum.StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REVOKED = "revoked"
    EXPIRED = "expired"


# --- scope (shared by every personal/family owned table) -------------------------------


class RecordScope(enum.StrEnum):
    """``personal``: visible to ``owner_user_id`` only (``family_id`` IS NULL).
    ``family``: visible to active members of ``family_id`` (``family_id`` NOT NULL)."""

    PERSONAL = "personal"
    FAMILY = "family"


# --- finance ---------------------------------------------------------------------------


class AccountType(enum.StrEnum):
    BANK = "bank"
    CASH = "cash"
    UPI = "upi"
    CREDIT_CARD = "credit_card"
    DEBIT_CARD = "debit_card"
    WALLET = "wallet"
    OTHER = "other"


class CategoryKind(enum.StrEnum):
    EXPENSE = "expense"
    INCOME = "income"


class TransactionType(enum.StrEnum):
    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"
    REFUND = "refund"
    ADJUSTMENT = "adjustment"


class TransactionStatus(enum.StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TransactionSource(enum.StrEnum):
    MANUAL = "manual"
    UPI_INTENT = "upi_intent"
    IMPORT = "import"
    RECURRING = "recurring"


class PaymentMethod(enum.StrEnum):
    CASH = "cash"
    UPI = "upi"
    CARD = "card"
    BANK_TRANSFER = "bank_transfer"
    WALLET = "wallet"
    OTHER = "other"


class VerificationStatus(enum.StrEnum):
    """How much we trust a payment outcome. ``user_confirmed`` = the user said so;
    ``verified`` = an independent trusted source (e.g. statement import). Never inferred from a
    UPI app merely opening."""

    UNVERIFIED = "unverified"
    USER_CONFIRMED = "user_confirmed"
    VERIFIED = "verified"


# --- planning --------------------------------------------------------------------------


class BudgetPeriod(enum.StrEnum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    CUSTOM = "custom"


class RecurrenceFrequency(enum.StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"


class GoalStatus(enum.StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class ContributionKind(enum.StrEnum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"


# --- UPI -------------------------------------------------------------------------------


class UpiAttemptStatus(enum.StrEnum):
    """Lifecycle of one UPI intent hand-off. ``*_reported`` = what the user / return intent
    claimed (untrusted). ``launched`` never implies success."""

    INITIATED = "initiated"
    LAUNCHED = "launched"
    SUCCESS_REPORTED = "success_reported"
    FAILURE_REPORTED = "failure_reported"
    PENDING_REPORTED = "pending_reported"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


# --- notifications ---------------------------------------------------------------------


class NotificationCategory(enum.StrEnum):
    BUDGET_ALERT = "budget_alert"
    BILL_UPCOMING = "bill_upcoming"
    RECURRING_REMINDER = "recurring_reminder"
    PAYMENT_CONFIRMATION = "payment_confirmation"
    UPI_PENDING = "upi_pending"
    FAMILY_INVITATION = "family_invitation"
    FAMILY_MEMBER_JOINED = "family_member_joined"
    FAMILY_SHARED_EXPENSE = "family_shared_expense"
    SAVINGS_MILESTONE = "savings_milestone"
    UNUSUAL_SPENDING = "unusual_spending"
    SYSTEM = "system"


class PushStatus(enum.StrEnum):
    PENDING = "pending"  # waiting for the worker (or scheduled_for)
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"  # push disabled by preference / no device tokens


class DevicePlatform(enum.StrEnum):
    IOS = "ios"
    ANDROID = "android"


# --- SQLAlchemy column types (one object per PostgreSQL type) ---------------------------

FAMILY_ROLE = pg_enum(FamilyRole, "family_role")
INVITATION_STATUS = pg_enum(InvitationStatus, "invitation_status")
RECORD_SCOPE = pg_enum(RecordScope, "record_scope")
ACCOUNT_TYPE = pg_enum(AccountType, "account_type")
CATEGORY_KIND = pg_enum(CategoryKind, "category_kind")
TRANSACTION_TYPE = pg_enum(TransactionType, "transaction_type")
TRANSACTION_STATUS = pg_enum(TransactionStatus, "transaction_status")
TRANSACTION_SOURCE = pg_enum(TransactionSource, "transaction_source")
PAYMENT_METHOD = pg_enum(PaymentMethod, "payment_method")
VERIFICATION_STATUS = pg_enum(VerificationStatus, "verification_status")
BUDGET_PERIOD = pg_enum(BudgetPeriod, "budget_period")
RECURRENCE_FREQUENCY = pg_enum(RecurrenceFrequency, "recurrence_frequency")
GOAL_STATUS = pg_enum(GoalStatus, "goal_status")
CONTRIBUTION_KIND = pg_enum(ContributionKind, "contribution_kind")
UPI_ATTEMPT_STATUS = pg_enum(UpiAttemptStatus, "upi_attempt_status")
NOTIFICATION_CATEGORY = pg_enum(NotificationCategory, "notification_category")
PUSH_STATUS = pg_enum(PushStatus, "push_status")
DEVICE_PLATFORM = pg_enum(DevicePlatform, "device_platform")

# Creation order for migrations (types first, dropped last in downgrade).
ALL_PG_ENUMS: tuple[Enum, ...] = (
    FAMILY_ROLE,
    INVITATION_STATUS,
    RECORD_SCOPE,
    ACCOUNT_TYPE,
    CATEGORY_KIND,
    TRANSACTION_TYPE,
    TRANSACTION_STATUS,
    TRANSACTION_SOURCE,
    PAYMENT_METHOD,
    VERIFICATION_STATUS,
    BUDGET_PERIOD,
    RECURRENCE_FREQUENCY,
    GOAL_STATUS,
    CONTRIBUTION_KIND,
    UPI_ATTEMPT_STATUS,
    NOTIFICATION_CATEGORY,
    PUSH_STATUS,
    DEVICE_PLATFORM,
)
