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
    with op.batch_alter_table("instruments") as batch:
        batch.add_column(sa.Column("sector_code", sa.String(8),
                                   sa.ForeignKey("sectors.code"), nullable=True))
    op.drop_column("instruments", "audit_info")
    op.drop_column("instruments", "state")
    op.drop_column("sectors", "group_type")
    op.drop_table("sector_memberships")
