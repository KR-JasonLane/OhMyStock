"""trade_runs.warnings — 트레이딩 판정 경고 영속(결정 #36, P6 Task 7c)

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-24

진입 탈락 사유·재시도 판정·부분 실패 경고가 종전에는 `/trade/status`의
프로세스 메모리에만 존재해 run 종료와 함께 소실됐다 — "그날 왜 진입하지
않았나"를 사후에 SQL로 물을 수 없었고(결정 #36 "분석하기 쉬운 데이터
적재" 위반), 2026-07-24 7b 관찰에서 실제로 진입 재시도 전이가 우연히 뜬
HTTP 스냅샷으로만 남는 상황이 확인됐다.

형식: **개행 구분 텍스트**(analysis_runs.warnings는 JSON 배열 문자열 —
같은 목적의 컬럼이지만 형식이 다르다, 아키텍트 T7c 정정). 이 컬럼의 주
용도가 raw SQL 가독성(psql에서 바로 읽는 사후 분석)이라 JSON이 아닌
개행 텍스트를 택했다 — 현재 이 값을 재파싱하는 소비자는 없다.
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trade_runs", sa.Column("warnings", sa.Text(),
                                          nullable=True))


def downgrade() -> None:
    op.drop_column("trade_runs", "warnings")
