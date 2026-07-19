"""analysis run advice column

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-19
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "analysis_runs",
        sa.Column("max_picks_advice", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_runs", "max_picks_advice")
