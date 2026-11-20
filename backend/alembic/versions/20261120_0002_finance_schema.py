"""finance schema

Tables for Phases 3-7: families/members/invitations, accounts, categories (+ the built-in
default categories), payees, transactions/tags/attachments, budgets, recurring transactions,
savings goals/contributions, UPI payment attempts, notifications/preferences/device tokens.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-20 13:42:53.607822
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# PostgreSQL enum types created by this revision. Several are shared by more than one table,
# so they are created once up front and every column uses ``create_type=False``.
ENUMS: dict[str, tuple[str, ...]] = {
    "family_role": ("owner", "admin", "member", "viewer"),
    "invitation_status": ("pending", "accepted", "rejected", "revoked", "expired"),
    "record_scope": ("personal", "family"),
    "account_type": ("bank", "cash", "upi", "credit_card", "debit_card", "wallet", "other"),
    "category_kind": ("expense", "income"),
    "transaction_type": ("expense", "income", "transfer", "refund", "adjustment"),
    "transaction_status": ("pending", "completed", "failed", "cancelled"),
    "transaction_source": ("manual", "upi_intent", "import", "recurring"),
    "payment_method": ("cash", "upi", "card", "bank_transfer", "wallet", "other"),
    "verification_status": ("unverified", "user_confirmed", "verified"),
    "budget_period": ("weekly", "monthly", "custom"),
    "recurrence_frequency": ("daily", "weekly", "monthly", "yearly"),
    "goal_status": ("active", "completed", "archived"),
    "contribution_kind": ("deposit", "withdrawal"),
    "upi_attempt_status": (
        "initiated",
        "launched",
        "success_reported",
        "failure_reported",
        "pending_reported",
        "cancelled",
        "expired",
    ),
    "notification_category": (
        "budget_alert",
        "bill_upcoming",
        "recurring_reminder",
        "payment_confirmation",
        "upi_pending",
        "family_invitation",
        "family_member_joined",
        "family_shared_expense",
        "savings_milestone",
        "unusual_spending",
        "system",
    ),
    "push_status": ("pending", "sent", "failed", "skipped"),
    "device_platform": ("ios", "android"),
}

# Built-in categories (owner_user_id IS NULL AND family_id IS NULL). IDs are stable (uuid5)
# so clients and later migrations can reference them. This is a frozen copy — migrations never
# import app code. Icons are Lucide icon names; colours are #RRGGBB.
# (id, kind, name, icon, color, sort_order)
DEFAULT_CATEGORIES: tuple[tuple[str, str, str, str, str, int], ...] = (
    ("97a3995f-d00d-5f96-8d0a-4fc98fe09b24", "expense", "Food & Dining", "utensils", "#F97316", 0),
    (
        "70170c58-f9af-52dc-9dca-00e5ed7bfdbd",
        "expense",
        "Groceries",
        "shopping-basket",
        "#22C55E",
        1,
    ),
    ("3f1aa228-a609-5b04-952c-694e67d8bb92", "expense", "Rent", "home", "#8B5CF6", 2),
    ("189a222d-ec62-5175-adfa-920aca04cf50", "expense", "Utilities", "plug", "#0EA5E9", 3),
    ("f7dc6fe8-c106-5a49-9424-5d13a45c95ab", "expense", "Electricity", "zap", "#EAB308", 4),
    ("192541f1-812b-5e85-90da-ca687e027a11", "expense", "Internet", "wifi", "#06B6D4", 5),
    (
        "fe3b7b5a-a171-548e-a978-d287c3d5870e",
        "expense",
        "Mobile Recharge",
        "smartphone",
        "#3B82F6",
        6,
    ),
    ("fe4fe30c-ce49-5932-9de4-ff2c81bf4639", "expense", "Transportation", "bus", "#6366F1", 7),
    ("edcd3651-4859-5fc2-89f2-6d5a14ccbf20", "expense", "Fuel", "fuel", "#EF4444", 8),
    ("dd347d22-3597-5276-a59d-798506c8b8ea", "expense", "Shopping", "shopping-bag", "#EC4899", 9),
    ("4f84fcb3-9e1c-5b6c-993d-a3818ff466e9", "expense", "Healthcare", "heart-pulse", "#F43F5E", 10),
    (
        "ce1efef2-64f3-5128-971d-ca41c668082f",
        "expense",
        "Education",
        "graduation-cap",
        "#14B8A6",
        11,
    ),
    ("bdd3ae6f-b1a3-5709-b0ab-ecba28cdd2f4", "expense", "Entertainment", "film", "#A855F7", 12),
    ("2d6c7cdb-1061-5d03-b26d-7af43e606c89", "expense", "Travel", "plane", "#0284C7", 13),
    ("939489a9-9c3f-51fc-9300-cfb4c9af995e", "expense", "Insurance", "shield", "#64748B", 14),
    ("faf36ccb-0819-5603-b6d8-f695e7d70c2c", "expense", "Subscriptions", "repeat", "#D946EF", 15),
    ("cd734d78-5a67-5669-8dc0-5975dac11514", "expense", "EMI & Loans", "landmark", "#B45309", 16),
    ("069b4dd4-e6bc-580e-822e-d28200b103cd", "expense", "Personal Care", "sparkles", "#F472B6", 17),
    ("657484fc-e852-5785-bf50-225e3b8c3b8a", "expense", "Family Expenses", "users", "#10B981", 18),
    ("88d4cc37-1a01-5d0f-8b6c-ff1dace6c419", "expense", "Other", "circle-ellipsis", "#94A3B8", 19),
    ("1542801b-8b21-58e8-a803-d63ba818b62d", "income", "Salary", "briefcase", "#16A34A", 0),
    ("7d32e4fa-ce02-5cf1-a0f5-212bfedf7ed3", "income", "Business", "store", "#0D9488", 1),
    ("bd170ed7-b4e1-59e5-8ec5-2bfeb67e46c3", "income", "Interest", "percent", "#2563EB", 2),
    ("fe6ad237-1497-5cdb-b27a-74552ac9c4c5", "income", "Gifts", "gift", "#DB2777", 3),
    ("f852037e-2b3e-56e7-98b9-3c13b47b23cb", "income", "Refunds", "rotate-ccw", "#7C3AED", 4),
    ("7371c38d-335f-5b42-8f81-64791cefda38", "income", "Other Income", "circle-plus", "#64748B", 5),
)

UPI_FK = "fk_transactions_upi_attempt_id_upi_payment_attempts"


def _categories_table() -> sa.TableClause:
    return sa.table(
        "transaction_categories",
        sa.column("id", sa.UUID()),
        sa.column("kind", postgresql.ENUM(name="category_kind", create_type=False)),
        sa.column("name", sa.String()),
        sa.column("icon", sa.String()),
        sa.column("color", sa.String()),
        sa.column("sort_order", sa.SmallInteger()),
    )


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=False)

    # ### commands auto generated by Alembic (reviewed) ###
    op.create_table(
        "families",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("avatar_key", sa.String(length=255), nullable=True),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], name=op.f("fk_families_created_by_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_families")),
    )
    op.create_table(
        "notification_preferences",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "category",
            postgresql.ENUM(
                "budget_alert",
                "bill_upcoming",
                "recurring_reminder",
                "payment_confirmation",
                "upi_pending",
                "family_invitation",
                "family_member_joined",
                "family_shared_expense",
                "savings_milestone",
                "unusual_spending",
                "system",
                name="notification_category",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("push_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("in_app_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_preferences_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "category", name=op.f("pk_notification_preferences")),
    )
    op.create_table(
        "accounts",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column(
            "type",
            postgresql.ENUM(
                "bank",
                "cash",
                "upi",
                "credit_card",
                "debit_card",
                "wallet",
                "other",
                name="account_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("institution", sa.String(length=80), nullable=True),
        sa.Column(
            "opening_balance",
            sa.Numeric(precision=14, scale=2),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=True),
        sa.Column(
            "scope",
            postgresql.ENUM("personal", "family", name="record_scope", create_type=False),
            server_default="personal",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(scope = 'personal' AND family_id IS NULL) OR (scope = 'family' AND family_id IS NOT NULL)",
            name=op.f("ck_accounts_scope_family"),
        ),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_accounts_family_id_families")
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], name=op.f("fk_accounts_owner_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
    )
    op.create_index("ix_accounts_family_id", "accounts", ["family_id"], unique=False)
    op.create_index("ix_accounts_owner_user_id", "accounts", ["owner_user_id"], unique=False)
    op.create_table(
        "budgets",
        sa.Column("member_user_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column(
            "period",
            postgresql.ENUM("weekly", "monthly", "custom", name="budget_period", create_type=False),
            nullable=False,
        ),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("carry_forward", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "alert_thresholds",
            postgresql.ARRAY(sa.SmallInteger()),
            server_default=sa.text("'{50,75,90,100}'"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=True),
        sa.Column(
            "scope",
            postgresql.ENUM("personal", "family", name="record_scope", create_type=False),
            server_default="personal",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(scope = 'personal' AND family_id IS NULL) OR (scope = 'family' AND family_id IS NOT NULL)",
            name=op.f("ck_budgets_scope_family"),
        ),
        sa.CheckConstraint(
            "member_user_id IS NULL OR scope = 'family'", name=op.f("ck_budgets_member_family_only")
        ),
        sa.CheckConstraint(
            "period <> 'custom' OR end_date IS NOT NULL", name=op.f("ck_budgets_custom_has_end")
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_budgets_amount_positive")),
        sa.CheckConstraint(
            "end_date IS NULL OR end_date >= start_date", name=op.f("ck_budgets_date_order")
        ),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_budgets_family_id_families")
        ),
        sa.ForeignKeyConstraint(
            ["member_user_id"], ["users.id"], name=op.f("fk_budgets_member_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], name=op.f("fk_budgets_owner_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budgets")),
    )
    op.create_index("ix_budgets_family_id", "budgets", ["family_id"], unique=False)
    op.create_index("ix_budgets_owner_user_id", "budgets", ["owner_user_id"], unique=False)
    op.create_table(
        "device_tokens",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("token", sa.String(length=255), nullable=False),
        sa.Column(
            "platform",
            postgresql.ENUM("ios", "android", name="device_platform", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["user_sessions.id"],
            name=op.f("fk_device_tokens_session_id_user_sessions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_device_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_tokens")),
        sa.UniqueConstraint("token", name=op.f("uq_device_tokens_token")),
    )
    op.create_index(
        "ix_device_tokens_user_active",
        "device_tokens",
        ["user_id"],
        unique=False,
        postgresql_where=sa.text("disabled_at IS NULL"),
    )
    op.create_table(
        "family_invitations",
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM(
                "owner", "admin", "member", "viewer", name="family_role", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("invited_by_user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending",
                "accepted",
                "rejected",
                "revoked",
                "expired",
                name="invitation_status",
                create_type=False,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_user_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role <> 'owner'", name=op.f("ck_family_invitations_role_not_owner")),
        sa.CheckConstraint(
            "email = lower(email)", name=op.f("ck_family_invitations_email_lowercase")
        ),
        sa.ForeignKeyConstraint(
            ["accepted_user_id"],
            ["users.id"],
            name=op.f("fk_family_invitations_accepted_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["family_id"],
            ["families.id"],
            name=op.f("fk_family_invitations_family_id_families"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_user_id"],
            ["users.id"],
            name=op.f("fk_family_invitations_invited_by_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_family_invitations")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_family_invitations_token_hash")),
    )
    op.create_index(
        "ix_family_invitations_email_status",
        "family_invitations",
        ["email", "status"],
        unique=False,
    )
    op.create_index(
        "uq_family_invitations_pending_email",
        "family_invitations",
        ["family_id", "email"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "family_members",
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM(
                "owner", "admin", "member", "viewer", name="family_role", create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "joined_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_by_user_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["family_id"],
            ["families.id"],
            name=op.f("fk_family_members_family_id_families"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["removed_by_user_id"],
            ["users.id"],
            name=op.f("fk_family_members_removed_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_family_members_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_family_members")),
    )
    op.create_index(
        "ix_family_members_user_active",
        "family_members",
        ["user_id"],
        unique=False,
        postgresql_where=sa.text("left_at IS NULL"),
    )
    op.create_index(
        "uq_family_members_active_owner",
        "family_members",
        ["family_id"],
        unique=True,
        postgresql_where=sa.text("role = 'owner' AND left_at IS NULL"),
    )
    op.create_index(
        "uq_family_members_active_user",
        "family_members",
        ["family_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("left_at IS NULL"),
    )
    op.create_table(
        "notifications",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=True),
        sa.Column(
            "category",
            postgresql.ENUM(
                "budget_alert",
                "bill_upcoming",
                "recurring_reminder",
                "payment_confirmation",
                "upi_pending",
                "family_invitation",
                "family_member_joined",
                "family_shared_expense",
                "savings_milestone",
                "unusual_spending",
                "system",
                name="notification_category",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("dedupe_key", sa.String(length=200), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "scheduled_for",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "push_status",
            postgresql.ENUM(
                "pending", "sent", "failed", "skipped", name="push_status", create_type=False
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["family_id"],
            ["families.id"],
            name=op.f("fk_notifications_family_id_families"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notifications_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint(
            "user_id", "dedupe_key", name=op.f("uq_notifications_user_id_dedupe_key")
        ),
    )
    op.create_index(
        "ix_notifications_push_due",
        "notifications",
        ["scheduled_for"],
        unique=False,
        postgresql_where=sa.text("push_status = 'pending'"),
    )
    op.create_index(
        "ix_notifications_user_created", "notifications", ["user_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_notifications_user_unread",
        "notifications",
        ["user_id"],
        unique=False,
        postgresql_where=sa.text("read_at IS NULL"),
    )
    op.create_table(
        "savings_goals",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("target_amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "active", "completed", "archived", name="goal_status", create_type=False
            ),
            server_default="active",
            nullable=False,
        ),
        sa.Column("icon", sa.String(length=40), nullable=True),
        sa.Column("color", sa.String(length=9), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=True),
        sa.Column(
            "scope",
            postgresql.ENUM("personal", "family", name="record_scope", create_type=False),
            server_default="personal",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(scope = 'personal' AND family_id IS NULL) OR (scope = 'family' AND family_id IS NOT NULL)",
            name=op.f("ck_savings_goals_scope_family"),
        ),
        sa.CheckConstraint("target_amount > 0", name=op.f("ck_savings_goals_target_positive")),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_savings_goals_family_id_families")
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], name=op.f("fk_savings_goals_owner_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_savings_goals")),
    )
    op.create_index("ix_savings_goals_family_id", "savings_goals", ["family_id"], unique=False)
    op.create_index(
        "ix_savings_goals_owner_user_id", "savings_goals", ["owner_user_id"], unique=False
    )
    op.create_table(
        "transaction_categories",
        sa.Column("owner_user_id", sa.UUID(), nullable=True),
        sa.Column("family_id", sa.UUID(), nullable=True),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("parent_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM("expense", "income", name="category_kind", create_type=False),
            nullable=False,
        ),
        sa.Column("icon", sa.String(length=40), nullable=True),
        sa.Column("color", sa.String(length=9), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "owner_user_id IS NULL OR family_id IS NULL",
            name=op.f("ck_transaction_categories_single_owner"),
        ),
        sa.CheckConstraint(
            "parent_id IS NULL OR parent_id <> id",
            name=op.f("ck_transaction_categories_parent_not_self"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_transaction_categories_created_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["family_id"],
            ["families.id"],
            name=op.f("fk_transaction_categories_family_id_families"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_transaction_categories_owner_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["transaction_categories.id"],
            name=op.f("fk_transaction_categories_parent_id_transaction_categories"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transaction_categories")),
    )
    op.create_index(
        "ix_transaction_categories_family_id", "transaction_categories", ["family_id"], unique=False
    )
    op.create_index(
        "ix_transaction_categories_parent_id", "transaction_categories", ["parent_id"], unique=False
    )
    op.create_index(
        "uq_transaction_categories_owner_name",
        "transaction_categories",
        ["owner_user_id", "family_id", "parent_id", "kind", sa.literal_column("lower(name)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
        postgresql_nulls_not_distinct=True,
    )
    op.create_table(
        "budget_categories",
        sa.Column("budget_id", sa.UUID(), nullable=False),
        sa.Column("category_id", sa.UUID(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_budget_categories_amount_positive")),
        sa.ForeignKeyConstraint(
            ["budget_id"],
            ["budgets.id"],
            name=op.f("fk_budget_categories_budget_id_budgets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["transaction_categories.id"],
            name=op.f("fk_budget_categories_category_id_transaction_categories"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_categories")),
        sa.UniqueConstraint(
            "budget_id", "category_id", name=op.f("uq_budget_categories_budget_id_category_id")
        ),
    )
    op.create_index(
        "ix_budget_categories_category_id", "budget_categories", ["category_id"], unique=False
    )
    op.create_table(
        "payees",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("upi_id", sa.String(length=255), nullable=True),
        sa.Column("mobile", sa.String(length=15), nullable=True),
        sa.Column("merchant_code", sa.String(length=10), nullable=True),
        sa.Column("default_category_id", sa.UUID(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("use_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=True),
        sa.Column(
            "scope",
            postgresql.ENUM("personal", "family", name="record_scope", create_type=False),
            server_default="personal",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(scope = 'personal' AND family_id IS NULL) OR (scope = 'family' AND family_id IS NOT NULL)",
            name=op.f("ck_payees_scope_family"),
        ),
        sa.CheckConstraint(
            "upi_id IS NULL OR upi_id = lower(upi_id)", name=op.f("ck_payees_upi_id_lowercase")
        ),
        sa.CheckConstraint("use_count >= 0", name=op.f("ck_payees_use_count_non_negative")),
        sa.ForeignKeyConstraint(
            ["default_category_id"],
            ["transaction_categories.id"],
            name=op.f("fk_payees_default_category_id_transaction_categories"),
        ),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_payees_family_id_families")
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], name=op.f("fk_payees_owner_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payees")),
    )
    op.create_index("ix_payees_family_id", "payees", ["family_id"], unique=False)
    op.create_index(
        "ix_payees_owner_last_used", "payees", ["owner_user_id", "last_used_at"], unique=False
    )
    op.create_index(
        "uq_payees_owner_upi_id",
        "payees",
        ["owner_user_id", "family_id", "upi_id"],
        unique=True,
        postgresql_where=sa.text("upi_id IS NOT NULL AND deleted_at IS NULL"),
        postgresql_nulls_not_distinct=True,
    )
    op.create_table(
        "recurring_transactions",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column(
            "type",
            postgresql.ENUM(
                "expense",
                "income",
                "transfer",
                "refund",
                "adjustment",
                name="transaction_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=True),
        sa.Column("to_account_id", sa.UUID(), nullable=True),
        sa.Column("category_id", sa.UUID(), nullable=True),
        sa.Column("payee_id", sa.UUID(), nullable=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column(
            "payment_method",
            postgresql.ENUM(
                "cash",
                "upi",
                "card",
                "bank_transfer",
                "wallet",
                "other",
                name="payment_method",
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column(
            "frequency",
            postgresql.ENUM(
                "daily",
                "weekly",
                "monthly",
                "yearly",
                name="recurrence_frequency",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("interval_count", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("next_due_date", sa.Date(), nullable=False),
        sa.Column(
            "reminder_days_before", sa.SmallInteger(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("auto_generate", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_paused", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("last_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=True),
        sa.Column(
            "scope",
            postgresql.ENUM("personal", "family", name="record_scope", create_type=False),
            server_default="personal",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(scope = 'personal' AND family_id IS NULL) OR (scope = 'family' AND family_id IS NOT NULL)",
            name=op.f("ck_recurring_transactions_scope_family"),
        ),
        sa.CheckConstraint(
            "(type = 'transfer' AND account_id IS NOT NULL AND to_account_id IS NOT NULL AND to_account_id <> account_id) OR (type <> 'transfer' AND to_account_id IS NULL)",
            name=op.f("ck_recurring_transactions_transfer_accounts"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_recurring_transactions_amount_positive")),
        sa.CheckConstraint(
            "end_date IS NULL OR end_date >= start_date",
            name=op.f("ck_recurring_transactions_date_order"),
        ),
        sa.CheckConstraint(
            "interval_count >= 1", name=op.f("ck_recurring_transactions_interval_positive")
        ),
        sa.CheckConstraint(
            "reminder_days_before >= 0",
            name=op.f("ck_recurring_transactions_reminder_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_recurring_transactions_account_id_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["transaction_categories.id"],
            name=op.f("fk_recurring_transactions_category_id_transaction_categories"),
        ),
        sa.ForeignKeyConstraint(
            ["family_id"],
            ["families.id"],
            name=op.f("fk_recurring_transactions_family_id_families"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_recurring_transactions_owner_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["payee_id"], ["payees.id"], name=op.f("fk_recurring_transactions_payee_id_payees")
        ),
        sa.ForeignKeyConstraint(
            ["to_account_id"],
            ["accounts.id"],
            name=op.f("fk_recurring_transactions_to_account_id_accounts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recurring_transactions")),
    )
    op.create_index(
        "ix_recurring_transactions_due",
        "recurring_transactions",
        ["next_due_date"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL AND is_paused = false"),
    )
    op.create_index(
        "ix_recurring_transactions_family_id", "recurring_transactions", ["family_id"], unique=False
    )
    op.create_index(
        "ix_recurring_transactions_owner_user_id",
        "recurring_transactions",
        ["owner_user_id"],
        unique=False,
    )
    op.create_table(
        "transactions",
        sa.Column(
            "type",
            postgresql.ENUM(
                "expense",
                "income",
                "transfer",
                "refund",
                "adjustment",
                name="transaction_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending",
                "completed",
                "failed",
                "cancelled",
                name="transaction_status",
                create_type=False,
            ),
            server_default="completed",
            nullable=False,
        ),
        sa.Column(
            "source",
            postgresql.ENUM(
                "manual",
                "upi_intent",
                "import",
                "recurring",
                name="transaction_source",
                create_type=False,
            ),
            server_default="manual",
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=True),
        sa.Column("to_account_id", sa.UUID(), nullable=True),
        sa.Column("category_id", sa.UUID(), nullable=True),
        sa.Column("payee_id", sa.UUID(), nullable=True),
        sa.Column("merchant", sa.String(length=120), nullable=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "payment_method",
            postgresql.ENUM(
                "cash",
                "upi",
                "card",
                "bank_transfer",
                "wallet",
                "other",
                name="payment_method",
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recurring_id", sa.UUID(), nullable=True),
        sa.Column("period_key", sa.String(length=20), nullable=True),
        sa.Column("upi_attempt_id", sa.UUID(), nullable=True),
        sa.Column("external_ref", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=100), nullable=True),
        sa.Column(
            "verification",
            postgresql.ENUM(
                "unverified",
                "user_confirmed",
                "verified",
                name="verification_status",
                create_type=False,
            ),
            server_default="unverified",
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=True),
        sa.Column(
            "scope",
            postgresql.ENUM("personal", "family", name="record_scope", create_type=False),
            server_default="personal",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(scope = 'personal' AND family_id IS NULL) OR (scope = 'family' AND family_id IS NOT NULL)",
            name=op.f("ck_transactions_scope_family"),
        ),
        sa.CheckConstraint(
            "(type = 'transfer' AND account_id IS NOT NULL AND to_account_id IS NOT NULL AND to_account_id <> account_id) OR (type <> 'transfer' AND to_account_id IS NULL)",
            name=op.f("ck_transactions_transfer_accounts"),
        ),
        sa.CheckConstraint(
            "(recurring_id IS NULL) = (period_key IS NULL)",
            name=op.f("ck_transactions_recurring_period"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_transactions_amount_positive")),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_transactions_account_id_accounts")
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["transaction_categories.id"],
            name=op.f("fk_transactions_category_id_transaction_categories"),
        ),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_transactions_family_id_families")
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], name=op.f("fk_transactions_owner_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["payee_id"], ["payees.id"], name=op.f("fk_transactions_payee_id_payees")
        ),
        sa.ForeignKeyConstraint(
            ["recurring_id"],
            ["recurring_transactions.id"],
            name=op.f("fk_transactions_recurring_id_recurring_transactions"),
        ),
        sa.ForeignKeyConstraint(
            ["to_account_id"], ["accounts.id"], name=op.f("fk_transactions_to_account_id_accounts")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transactions")),
        sa.UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name=op.f("uq_transactions_owner_user_id_idempotency_key"),
        ),
        sa.UniqueConstraint(
            "recurring_id", "period_key", name=op.f("uq_transactions_recurring_id_period_key")
        ),
        sa.UniqueConstraint("upi_attempt_id", name=op.f("uq_transactions_upi_attempt_id")),
    )
    op.create_index("ix_transactions_account_id", "transactions", ["account_id"], unique=False)
    op.create_index("ix_transactions_category_id", "transactions", ["category_id"], unique=False)
    op.create_index(
        "ix_transactions_family_occurred",
        "transactions",
        ["family_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_transactions_owner_occurred",
        "transactions",
        ["owner_user_id", "occurred_at"],
        unique=False,
    )
    op.create_index("ix_transactions_payee_id", "transactions", ["payee_id"], unique=False)
    op.create_index(
        "ix_transactions_to_account_id",
        "transactions",
        ["to_account_id"],
        unique=False,
        postgresql_where=sa.text("to_account_id IS NOT NULL"),
    )
    op.create_table(
        "savings_contributions",
        sa.Column("goal_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM("deposit", "withdrawal", name="contribution_kind", create_type=False),
            server_default="deposit",
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "contributed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("transaction_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount > 0", name=op.f("ck_savings_contributions_amount_positive")),
        sa.ForeignKeyConstraint(
            ["goal_id"],
            ["savings_goals.id"],
            name=op.f("fk_savings_contributions_goal_id_savings_goals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name=op.f("fk_savings_contributions_transaction_id_transactions"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_savings_contributions_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_savings_contributions")),
    )
    op.create_index(
        "ix_savings_contributions_goal_contributed",
        "savings_contributions",
        ["goal_id", "contributed_at"],
        unique=False,
    )
    op.create_index(
        "ix_savings_contributions_transaction_id",
        "savings_contributions",
        ["transaction_id"],
        unique=False,
        postgresql_where=sa.text("transaction_id IS NOT NULL"),
    )
    op.create_table(
        "transaction_attachments",
        sa.Column("transaction_id", sa.UUID(), nullable=False),
        sa.Column("storage_key", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("uploaded_by_user_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_transaction_attachments_size_positive")),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name=op.f("fk_transaction_attachments_transaction_id_transactions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_user_id"],
            ["users.id"],
            name=op.f("fk_transaction_attachments_uploaded_by_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transaction_attachments")),
        sa.UniqueConstraint("storage_key", name=op.f("uq_transaction_attachments_storage_key")),
    )
    op.create_index(
        "ix_transaction_attachments_transaction_id",
        "transaction_attachments",
        ["transaction_id"],
        unique=False,
    )
    op.create_table(
        "transaction_tags",
        sa.Column("transaction_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("name = lower(name)", name=op.f("ck_transaction_tags_name_lowercase")),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name=op.f("fk_transaction_tags_transaction_id_transactions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("transaction_id", "name", name=op.f("pk_transaction_tags")),
    )
    op.create_index("ix_transaction_tags_name", "transaction_tags", ["name"], unique=False)
    op.create_table(
        "upi_payment_attempts",
        sa.Column("transaction_id", sa.UUID(), nullable=True),
        sa.Column("payee_id", sa.UUID(), nullable=True),
        sa.Column("payee_vpa", sa.String(length=255), nullable=False),
        sa.Column("payee_name", sa.String(length=120), nullable=True),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("note", sa.String(length=80), nullable=True),
        sa.Column("tr_ref", sa.String(length=35), nullable=False),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "initiated",
                "launched",
                "success_reported",
                "failure_reported",
                "pending_reported",
                "cancelled",
                "expired",
                name="upi_attempt_status",
                create_type=False,
            ),
            server_default="initiated",
            nullable=False,
        ),
        sa.Column(
            "verification",
            postgresql.ENUM(
                "unverified",
                "user_confirmed",
                "verified",
                name="verification_status",
                create_type=False,
            ),
            server_default="unverified",
            nullable=False,
        ),
        sa.Column("utr", sa.String(length=64), nullable=True),
        sa.Column("raw_return", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "initiated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("launched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=True),
        sa.Column(
            "scope",
            postgresql.ENUM("personal", "family", name="record_scope", create_type=False),
            server_default="personal",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(scope = 'personal' AND family_id IS NULL) OR (scope = 'family' AND family_id IS NOT NULL)",
            name=op.f("ck_upi_payment_attempts_scope_family"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_upi_payment_attempts_amount_positive")),
        sa.CheckConstraint(
            "payee_vpa = lower(payee_vpa)", name=op.f("ck_upi_payment_attempts_payee_vpa_lowercase")
        ),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_upi_payment_attempts_family_id_families")
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_upi_payment_attempts_owner_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["payee_id"], ["payees.id"], name=op.f("fk_upi_payment_attempts_payee_id_payees")
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name=op.f("fk_upi_payment_attempts_transaction_id_transactions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_upi_payment_attempts")),
        sa.UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name=op.f("uq_upi_payment_attempts_owner_user_id_idempotency_key"),
        ),
        sa.UniqueConstraint("tr_ref", name=op.f("uq_upi_payment_attempts_tr_ref")),
        sa.UniqueConstraint("transaction_id", name=op.f("uq_upi_payment_attempts_transaction_id")),
    )
    op.create_index(
        "ix_upi_payment_attempts_family_id", "upi_payment_attempts", ["family_id"], unique=False
    )
    op.create_index(
        "ix_upi_payment_attempts_open",
        "upi_payment_attempts",
        ["initiated_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('initiated', 'launched', 'pending_reported')"),
    )
    op.create_index(
        "ix_upi_payment_attempts_owner_initiated",
        "upi_payment_attempts",
        ["owner_user_id", "initiated_at"],
        unique=False,
    )
    # FK cycle transactions <-> upi_payment_attempts: added once both tables exist.
    op.create_foreign_key(
        op.f(UPI_FK), "transactions", "upi_payment_attempts", ["upi_attempt_id"], ["id"]
    )
    # ### end Alembic commands ###

    op.bulk_insert(
        _categories_table(),
        [
            {
                "id": id_,
                "kind": kind,
                "name": name,
                "icon": icon,
                "color": color,
                "sort_order": order,
            }
            for id_, kind, name, icon, color, order in DEFAULT_CATEGORIES
        ],
    )


def downgrade() -> None:
    # Seeded categories go away with the table below; deleting them first would fail with an
    # FK violation whenever transactions/budgets reference them.
    op.drop_constraint(op.f(UPI_FK), "transactions", type_="foreignkey")
    # ### commands auto generated by Alembic (reviewed) ###
    op.drop_index("ix_upi_payment_attempts_owner_initiated", table_name="upi_payment_attempts")
    op.drop_index(
        "ix_upi_payment_attempts_open",
        table_name="upi_payment_attempts",
        postgresql_where=sa.text("status IN ('initiated', 'launched', 'pending_reported')"),
    )
    op.drop_index("ix_upi_payment_attempts_family_id", table_name="upi_payment_attempts")
    op.drop_table("upi_payment_attempts")
    op.drop_index("ix_transaction_tags_name", table_name="transaction_tags")
    op.drop_table("transaction_tags")
    op.drop_index("ix_transaction_attachments_transaction_id", table_name="transaction_attachments")
    op.drop_table("transaction_attachments")
    op.drop_index(
        "ix_savings_contributions_transaction_id",
        table_name="savings_contributions",
        postgresql_where=sa.text("transaction_id IS NOT NULL"),
    )
    op.drop_index("ix_savings_contributions_goal_contributed", table_name="savings_contributions")
    op.drop_table("savings_contributions")
    op.drop_index(
        "ix_transactions_to_account_id",
        table_name="transactions",
        postgresql_where=sa.text("to_account_id IS NOT NULL"),
    )
    op.drop_index("ix_transactions_payee_id", table_name="transactions")
    op.drop_index("ix_transactions_owner_occurred", table_name="transactions")
    op.drop_index("ix_transactions_family_occurred", table_name="transactions")
    op.drop_index("ix_transactions_category_id", table_name="transactions")
    op.drop_index("ix_transactions_account_id", table_name="transactions")
    op.drop_table("transactions")
    op.drop_index("ix_recurring_transactions_owner_user_id", table_name="recurring_transactions")
    op.drop_index("ix_recurring_transactions_family_id", table_name="recurring_transactions")
    op.drop_index(
        "ix_recurring_transactions_due",
        table_name="recurring_transactions",
        postgresql_where=sa.text("deleted_at IS NULL AND is_paused = false"),
    )
    op.drop_table("recurring_transactions")
    op.drop_index(
        "uq_payees_owner_upi_id",
        table_name="payees",
        postgresql_where=sa.text("upi_id IS NOT NULL AND deleted_at IS NULL"),
        postgresql_nulls_not_distinct=True,
    )
    op.drop_index("ix_payees_owner_last_used", table_name="payees")
    op.drop_index("ix_payees_family_id", table_name="payees")
    op.drop_table("payees")
    op.drop_index("ix_budget_categories_category_id", table_name="budget_categories")
    op.drop_table("budget_categories")
    op.drop_index(
        "uq_transaction_categories_owner_name",
        table_name="transaction_categories",
        postgresql_where=sa.text("deleted_at IS NULL"),
        postgresql_nulls_not_distinct=True,
    )
    op.drop_index("ix_transaction_categories_parent_id", table_name="transaction_categories")
    op.drop_index("ix_transaction_categories_family_id", table_name="transaction_categories")
    op.drop_table("transaction_categories")
    op.drop_index("ix_savings_goals_owner_user_id", table_name="savings_goals")
    op.drop_index("ix_savings_goals_family_id", table_name="savings_goals")
    op.drop_table("savings_goals")
    op.drop_index(
        "ix_notifications_user_unread",
        table_name="notifications",
        postgresql_where=sa.text("read_at IS NULL"),
    )
    op.drop_index("ix_notifications_user_created", table_name="notifications")
    op.drop_index(
        "ix_notifications_push_due",
        table_name="notifications",
        postgresql_where=sa.text("push_status = 'pending'"),
    )
    op.drop_table("notifications")
    op.drop_index(
        "uq_family_members_active_user",
        table_name="family_members",
        postgresql_where=sa.text("left_at IS NULL"),
    )
    op.drop_index(
        "uq_family_members_active_owner",
        table_name="family_members",
        postgresql_where=sa.text("role = 'owner' AND left_at IS NULL"),
    )
    op.drop_index(
        "ix_family_members_user_active",
        table_name="family_members",
        postgresql_where=sa.text("left_at IS NULL"),
    )
    op.drop_table("family_members")
    op.drop_index(
        "uq_family_invitations_pending_email",
        table_name="family_invitations",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index("ix_family_invitations_email_status", table_name="family_invitations")
    op.drop_table("family_invitations")
    op.drop_index(
        "ix_device_tokens_user_active",
        table_name="device_tokens",
        postgresql_where=sa.text("disabled_at IS NULL"),
    )
    op.drop_table("device_tokens")
    op.drop_index("ix_budgets_owner_user_id", table_name="budgets")
    op.drop_index("ix_budgets_family_id", table_name="budgets")
    op.drop_table("budgets")
    op.drop_index("ix_accounts_owner_user_id", table_name="accounts")
    op.drop_index("ix_accounts_family_id", table_name="accounts")
    op.drop_table("accounts")
    op.drop_table("notification_preferences")
    op.drop_table("families")
    # ### end Alembic commands ###
    bind = op.get_bind()
    for name in reversed(ENUMS):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
