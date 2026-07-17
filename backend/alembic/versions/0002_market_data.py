"""market data tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sectors",
        sa.Column("code", sa.String(8), primary_key=True),
        sa.Column("market", sa.String(16), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
    )
    op.create_table(
        "instruments",
        sa.Column("symbol", sa.String(12), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("market", sa.String(16), nullable=False),
        sa.Column("instrument_type", sa.String(32), nullable=False, server_default=""),
        sa.Column("sector_code", sa.String(8), sa.ForeignKey("sectors.code"),
                  nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "candles",
        sa.Column("symbol", sa.String(12), primary_key=True),
        sa.Column("date", sa.Date, primary_key=True),
        sa.Column("open", sa.Integer, nullable=False),
        sa.Column("high", sa.Integer, nullable=False),
        sa.Column("low", sa.Integer, nullable=False),
        sa.Column("close", sa.Integer, nullable=False),
        sa.Column("volume", sa.BigInteger, nullable=False),
    )
    op.create_table(
        "collection_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("total_symbols", sa.Integer, nullable=False, server_default="0"),
        sa.Column("succeeded", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_summary", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("collection_runs")
    op.drop_table("candles")
    op.drop_table("instruments")
    op.drop_table("sectors")
