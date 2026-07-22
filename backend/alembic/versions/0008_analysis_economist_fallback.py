"""analysis_runs.economist_fallback (P5 이월 — 0007과 별도 리비전, 규칙 4)

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-22

economist 파싱 실패 폴백(중립+상한 5, 결정 #23으로 열어둠) 발동 여부를 DB에서
구분할 수 있게 한다 — 폴백 유지 결정으로 감사 중요도가 올라간 항목(사전 게이트
회고록 §5 이월). 트레이딩과 무관한 analysis 스키마라 0007에 섞지 않는다.
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "analysis_runs",
        sa.Column("economist_fallback", sa.Boolean(), nullable=False,
                  server_default="0"))


def downgrade() -> None:
    op.drop_column("analysis_runs", "economist_fallback")
