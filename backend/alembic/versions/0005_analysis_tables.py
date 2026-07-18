"""analysis result tables

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analysis_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("score_run_id", sa.Integer,
                  sa.ForeignKey("score_runs.id"), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("prompt_hash", sa.String(16), nullable=False),
        sa.Column("config", sa.Text, nullable=False, server_default="{}"),
        sa.Column("regime", sa.String(16), nullable=True),
        sa.Column("market_summary", sa.Text, nullable=True),
        sa.Column("warnings", sa.Text, nullable=True),
        sa.Column("failure_reason", sa.Text, nullable=True),
    )
    op.create_table(
        "analysis_verdicts",
        sa.Column("run_id", sa.Integer,
                  sa.ForeignKey("analysis_runs.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("symbol", sa.String(12), primary_key=True),
        sa.Column("verdict", sa.String(8), nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("reasons", sa.Text, nullable=False),
        sa.Column("risk_flags", sa.Text, nullable=False),
        sa.Column("picked", sa.Boolean, nullable=False),
        sa.Column("pick_rank", sa.Integer, nullable=True),
    )
    op.create_table(
        "analysis_news",
        sa.Column("run_id", sa.Integer,
                  sa.ForeignKey("analysis_runs.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("scope", sa.String(12), primary_key=True),
        sa.Column("url", sa.String(512), primary_key=True),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("published_at", sa.String(64), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("analysis_news")
    op.drop_table("analysis_verdicts")
    op.drop_table("analysis_runs")
