"""Persist latest observed prices for managed trade positions.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("trade_positions") as batch:
        batch.add_column(sa.Column("mark_price", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("marked_at", sa.DateTime(timezone=True),
                                   nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("trade_positions") as batch:
        batch.drop_column("marked_at")
        batch.drop_column("mark_price")
