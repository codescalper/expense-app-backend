"""Budgets, recurring transactions and savings goals (Phase 6)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPkMixin
from app.models.enums import (
    BUDGET_PERIOD,
    CONTRIBUTION_KIND,
    GOAL_STATUS,
    PAYMENT_METHOD,
    RECURRENCE_FREQUENCY,
    TRANSACTION_TYPE,
    BudgetPeriod,
    ContributionKind,
    GoalStatus,
    PaymentMethod,
    RecurrenceFrequency,
    TransactionType,
)
from app.models.mixins import ScopedOwnershipMixin, scope_family_check

DEFAULT_ALERT_THRESHOLDS = [50, 75, 90, 100]


class Budget(UUIDPkMixin, ScopedOwnershipMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Spending limit for a period. ``member_user_id`` (family scope only) restricts the budget
    to one member's spending. Weekly/monthly budgets recur from ``start_date``; ``custom``
    budgets cover ``start_date``..``end_date`` once."""

    __tablename__ = "budgets"
    __table_args__ = (
        scope_family_check(),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("member_user_id IS NULL OR scope = 'family'", name="member_family_only"),
        CheckConstraint("period <> 'custom' OR end_date IS NOT NULL", name="custom_has_end"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="date_order"),
        Index("ix_budgets_owner_user_id", "owner_user_id"),
        Index("ix_budgets_family_id", "family_id"),
    )

    member_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(80))
    period: Mapped[BudgetPeriod] = mapped_column(BUDGET_PERIOD)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    amount: Mapped[Decimal]
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")
    carry_forward: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    alert_thresholds: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger),
        default=lambda: list(DEFAULT_ALERT_THRESHOLDS),
        server_default=text("'{50,75,90,100}'"),
    )
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))


class BudgetCategory(UUIDPkMixin, TimestampMixin, Base):
    """Per-category limit inside a budget."""

    __tablename__ = "budget_categories"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        UniqueConstraint("budget_id", "category_id"),
        Index("ix_budget_categories_category_id", "category_id"),
    )

    budget_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("budgets.id", ondelete="CASCADE"))
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transaction_categories.id"))
    amount: Mapped[Decimal]


class RecurringTransaction(
    UUIDPkMixin, ScopedOwnershipMixin, TimestampMixin, SoftDeleteMixin, Base
):
    """Template for bills / subscriptions / salary. The worker generates **pending**
    transactions keyed by ``(recurring_id, period_key)`` — never auto-marked as paid."""

    __tablename__ = "recurring_transactions"
    __table_args__ = (
        scope_family_check(),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("interval_count >= 1", name="interval_positive"),
        CheckConstraint("reminder_days_before >= 0", name="reminder_non_negative"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="date_order"),
        CheckConstraint(
            "(type = 'transfer' AND account_id IS NOT NULL AND to_account_id IS NOT NULL "
            "AND to_account_id <> account_id) OR (type <> 'transfer' AND to_account_id IS NULL)",
            name="transfer_accounts",
        ),
        Index("ix_recurring_transactions_owner_user_id", "owner_user_id"),
        Index("ix_recurring_transactions_family_id", "family_id"),
        # Worker scan: live, unpaused rules by due date.
        Index(
            "ix_recurring_transactions_due",
            "next_due_date",
            postgresql_where=text("deleted_at IS NULL AND is_paused = false"),
        ),
    )

    name: Mapped[str] = mapped_column(String(80))
    type: Mapped[TransactionType] = mapped_column(TRANSACTION_TYPE)
    amount: Mapped[Decimal]
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    to_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("transaction_categories.id"))
    payee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payees.id"))
    description: Mapped[str | None] = mapped_column(String(255))
    payment_method: Mapped[PaymentMethod | None] = mapped_column(PAYMENT_METHOD)
    frequency: Mapped[RecurrenceFrequency] = mapped_column(RECURRENCE_FREQUENCY)
    interval_count: Mapped[int] = mapped_column(SmallInteger, default=1, server_default=text("1"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    next_due_date: Mapped[date] = mapped_column(Date)
    reminder_days_before: Mapped[int] = mapped_column(
        SmallInteger, default=1, server_default=text("1")
    )
    auto_generate: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    is_paused: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    last_generated_at: Mapped[datetime | None]


class SavingsGoal(UUIDPkMixin, ScopedOwnershipMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Current amount = sum(deposits) - sum(withdrawals) of live contributions (computed)."""

    __tablename__ = "savings_goals"
    __table_args__ = (
        scope_family_check(),
        CheckConstraint("target_amount > 0", name="target_positive"),
        Index("ix_savings_goals_owner_user_id", "owner_user_id"),
        Index("ix_savings_goals_family_id", "family_id"),
    )

    name: Mapped[str] = mapped_column(String(80))
    target_amount: Mapped[Decimal]
    target_date: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")
    status: Mapped[GoalStatus] = mapped_column(
        GOAL_STATUS, default=GoalStatus.ACTIVE, server_default=GoalStatus.ACTIVE.value
    )
    icon: Mapped[str | None] = mapped_column(String(40))
    color: Mapped[str | None] = mapped_column(String(9))
    completed_at: Mapped[datetime | None]


class SavingsContribution(UUIDPkMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "savings_contributions"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_savings_contributions_goal_contributed", "goal_id", "contributed_at"),
        Index(
            "ix_savings_contributions_transaction_id",
            "transaction_id",
            postgresql_where=text("transaction_id IS NOT NULL"),
        ),
    )

    goal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("savings_goals.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[ContributionKind] = mapped_column(
        CONTRIBUTION_KIND,
        default=ContributionKind.DEPOSIT,
        server_default=ContributionKind.DEPOSIT.value,
    )
    amount: Mapped[Decimal]
    note: Mapped[str | None] = mapped_column(Text)
    contributed_at: Mapped[datetime] = mapped_column(server_default=func.now())
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("transactions.id"))
