"""sector memberships + instrument status fields

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sector_memberships",
        sa.Column("sector_code", sa.String(8), sa.ForeignKey("sectors.code"),
                  primary_key=True),
        sa.Column("symbol", sa.String(12), sa.ForeignKey("instruments.symbol"),
                  primary_key=True),
    )
    op.add_column("sectors", sa.Column("group_type", sa.String(24),
                                       nullable=False,
                                       server_default="unclassified"))
    op.add_column("instruments", sa.Column("state", sa.String(128),
                                           nullable=False, server_default=""))
    op.add_column("instruments", sa.Column("audit_info", sa.String(32),
                                           nullable=False, server_default=""))
    # sector_code는 last-write-wins로 손상된 라벨 (2026-07-18 실측) — 소비자
    # 없는 지금 제거. batch_alter_table은 sqlite(테스트) 호환용.
    with op.batch_alter_table("instruments") as batch:
        batch.drop_column("sector_code")


def downgrade() -> None:
    # instruments 칼럼 추가/삭제 전부를 batch_alter_table로 묶는다 — sqlite는
    # ALTER TABLE을 부분 지원하지 않아 recreate 경로를 타는데, 개별 op를
    # 섞으면(upgrade와의 비대칭) 테스트(sqlite)에서 깨진다.
    with op.batch_alter_table("instruments") as batch:
        # batch 모드(sqlite recreate)는 이름 없는 FK를 허용하지 않는다 —
        # 명시적으로 이름을 지정.
        batch.add_column(sa.Column("sector_code", sa.String(8), nullable=True))
        batch.create_foreign_key(
            "fk_instruments_sector_code_sectors", "sectors",
            ["sector_code"], ["code"])
        batch.drop_column("audit_info")
        batch.drop_column("state")
    op.drop_column("sectors", "group_type")
    op.drop_table("sector_memberships")
