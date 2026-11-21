"""periodic_task_runs: last-run bookkeeping for the cron tick (serverless deployments)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "periodic_task_runs",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_status", sa.String(length=16), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_periodic_task_runs")),
    )


def downgrade() -> None:
    op.drop_table("periodic_task_runs")
