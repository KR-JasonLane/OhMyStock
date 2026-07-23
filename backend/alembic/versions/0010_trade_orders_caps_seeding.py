"""trade_orders 시딩 지원 — est_krw 컬럼 + (trade_run_id, created_at) 인덱스

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-23

P6 Task 1(같은 날 재기동 시 일일 한도 DB 시딩, 스펙 §5-1)의 저장 지원 2건:

- ``est_krw``: 발주 시점 한도(caps.check) 추정 금액. 시장가 주문은
  req_price=0이고 record_fill이 프로덕션에 미배선이라, 이 컬럼 없이는
  재기동 시딩에서 시장가(지정가 타임아웃 폴백 진입·손절·킬스위치 청산)
  금액이 전부 0으로 빠져 일일 KRW 캡이 실질 무력화된다(P6-T1 패널
  트레이더 Critical). 기존 행은 0(과소계상 방향이지만 과거 행은 당일
  재기동 시딩 대상이 아니게 되는 시점에만 존재 — 배포 당일 1회 한정 허용).
- ``ix_trade_orders_run_created``: daily_order_usage의 프리필터(created_at
  범위 + trade_run_id 조인)가 insert-only 테이블 성장에도 스캔 상한을
  갖도록(P6-T1 패널 아키텍트 Important — FK 컬럼은 Postgres가 자동
  인덱싱하지 않는다).
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trade_orders",
        sa.Column("est_krw", sa.BigInteger(), nullable=False,
                  server_default="0"))
    op.create_index("ix_trade_orders_run_created", "trade_orders",
                    ["trade_run_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_trade_orders_run_created", table_name="trade_orders")
    op.drop_column("trade_orders", "est_krw")
