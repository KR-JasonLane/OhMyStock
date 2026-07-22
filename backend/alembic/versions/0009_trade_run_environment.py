"""trade_runs.run_environment (리플레이 프로필 감사 분리 — 스펙 §4-1)

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-22

리플레이 목 서버 런의 가짜 체결이 P&L/리스크 집계를 오염하지 않도록,
런이 어느 환경(mock/real/replay)을 향해 실행됐는지 NOT NULL 컬럼으로
기록한다(보안 패널 #4 — JSON 파싱 없이 구조적 필터). 기존 행은 전부
mock 런이므로 server_default="mock"이 사실과 일치한다.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trade_runs",
        sa.Column("run_environment", sa.String(length=16), nullable=False,
                  server_default="mock"))


def downgrade() -> None:
    op.drop_column("trade_runs", "run_environment")
