"""scoring result tables

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "score_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reference_date", sa.Date, nullable=False),
        sa.Column("universe_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("stale_excluded", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failure_reason", sa.Text, nullable=True),
        sa.Column("config", sa.Text, nullable=False, server_default="{}"),
    )
    op.create_table(
        "score_sectors",
        sa.Column("run_id", sa.Integer,
                  sa.ForeignKey("score_runs.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("sector_code", sa.String(8), primary_key=True),
        sa.Column("r20", sa.Float, nullable=False),
        sa.Column("r60", sa.Float, nullable=False),
        sa.Column("r5", sa.Float, nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("rank", sa.Integer, nullable=False),
        sa.Column("selected", sa.Boolean, nullable=False),
    )
    op.create_table(
        "scores",
        sa.Column("run_id", sa.Integer,
                  sa.ForeignKey("score_runs.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("symbol", sa.String(12), primary_key=True),
        sa.Column("rank", sa.Integer, nullable=False),
        sa.Column("total_score", sa.Float, nullable=False),
        sa.Column("sector_code", sa.String(8), nullable=False),
        sa.Column("sector_score", sa.Float, nullable=False),
        sa.Column("strategy_score", sa.Float, nullable=False),
        sa.Column("strategy_score_norm", sa.Float, nullable=False),
    )
    op.create_table(
        "score_details",
        sa.Column("run_id", sa.Integer,
                  sa.ForeignKey("score_runs.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("symbol", sa.String(12), primary_key=True),
        sa.Column("strategy", sa.String(32), primary_key=True),
        sa.Column("signal", sa.Boolean, nullable=False),
        sa.Column("avg_return", sa.Float, nullable=False),
        sa.Column("win_rate", sa.Float, nullable=False),
        sa.Column("occurrences", sa.Integer, nullable=False),
        sa.Column("score", sa.Float, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("score_details")
    op.drop_table("scores")
    op.drop_table("score_sectors")
    op.drop_table("score_runs")
