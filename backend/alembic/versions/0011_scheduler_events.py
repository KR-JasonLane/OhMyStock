"""scheduler_events — 스케줄러 판정 감사 테이블(P6 스펙 §6, 결정 #36)

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-23

insert-only. "왜 그 시각에 그 잡이 돌았나/안 돌았나"를 SQL로 복기 가능하게
한다. run_id는 job 값에 따라 4개 run 테이블 중 하나를 가리키는 폴리모픽
참조라 FK를 걸지 않는다(의도 — 단일 FK 시도 금지). reason은 도메인 enum
고정 리터럴만(자유 텍스트 금지 — 무인증 노출 표면). 조회는 최근 N건
(id desc)뿐이라 PK 인덱스로 충분 — 추가 인덱스 없음(YAGNI).
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduler_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("job", sa.String(length=8), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("scheduler_events")
