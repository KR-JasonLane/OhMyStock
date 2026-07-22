"""trading tables (P5 — trade_runs/positions/orders/fills)

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-22

전 FK 비-CASCADE — 실거래 감사 자산은 연쇄 삭제 금지(스펙 §9).
생성 순서: trade_runs → trade_positions → trade_orders → trade_fills
(orders.trade_position_id가 positions를 참조하므로 positions 선행).
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trade_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("config", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("stopped_by_kill_switch", sa.Boolean(), nullable=False,
                  server_default="0"),
        sa.Column("kill_switch_mode", sa.String(16), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
    )
    op.create_table(
        "trade_positions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("trade_run_id", sa.Integer(),
                  sa.ForeignKey("trade_runs.id"), nullable=False),
        sa.Column("symbol", sa.String(12), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("market", sa.String(8), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("entry_phase", sa.String(20), nullable=True),
        sa.Column("exit_phase", sa.String(20), nullable=True),
        sa.Column("entry_price", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("peak_price", sa.Integer(), nullable=False),
        sa.Column("trailing_active", sa.Boolean(), nullable=False,
                  server_default="0"),
        sa.Column("exit_price", sa.Integer(), nullable=True),
        sa.Column("exit_reason", sa.String(20), nullable=True),
        sa.Column("realized_pnl", sa.Integer(), nullable=True),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_trade_positions_state", "trade_positions", ["state"])
    op.create_table(
        "trade_orders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("trade_run_id", sa.Integer(),
                  sa.ForeignKey("trade_runs.id"), nullable=False),
        sa.Column("trade_position_id", sa.Integer(),
                  sa.ForeignKey("trade_positions.id"), nullable=True),
        sa.Column("order_no", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(12), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("order_style", sa.String(8), nullable=False),
        sa.Column("req_price", sa.Integer(), nullable=False),
        sa.Column("req_qty", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("resp_body", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_trade_orders_order_no", "trade_orders", ["order_no"])
    op.create_table(
        "trade_fills",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("order_id", sa.Integer(),
                  sa.ForeignKey("trade_orders.id"), nullable=False),
        sa.Column("fill_price", sa.Integer(), nullable=False),
        sa.Column("fill_qty", sa.Integer(), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("trade_fills")
    op.drop_index("ix_trade_orders_order_no", table_name="trade_orders")
    op.drop_table("trade_orders")
    op.drop_index("ix_trade_positions_state", table_name="trade_positions")
    op.drop_table("trade_positions")
    op.drop_table("trade_runs")
