"""transactions.adjustment_sign: direction of balance adjustments

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("transactions", sa.Column("adjustment_sign", sa.SmallInteger(), nullable=True))
    # Existing adjustments (none expected before this revision) default to raising the balance.
    op.execute("UPDATE transactions SET adjustment_sign = 1 WHERE type = 'adjustment'")
    op.create_check_constraint(
        op.f("ck_transactions_adjustment_sign"),
        "transactions",
        "(type = 'adjustment') = (adjustment_sign IS NOT NULL) "
        "AND (adjustment_sign IS NULL OR adjustment_sign IN (-1, 1))",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_transactions_adjustment_sign"), "transactions", type_="check")
    op.drop_column("transactions", "adjustment_sign")
