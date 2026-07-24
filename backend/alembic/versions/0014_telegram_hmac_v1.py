"""Widen Telegram external hashes for the explicit ``v1:`` prefix."""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


_COLUMNS = (
    ("telegram_updates", "operator_hash"),
    ("telegram_confirmations", "operator_hash"),
    ("telegram_confirmations", "chat_hash"),
    ("telegram_confirmation_locks", "operator_hash"),
    ("telegram_rejected_update_counters", "subject_hash"),
)


def upgrade() -> None:
    for table, column in _COLUMNS:
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                column,
                existing_type=sa.String(64),
                type_=sa.String(67),
                existing_nullable=False,
            )


def downgrade() -> None:
    for table, column in reversed(_COLUMNS):
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                column,
                existing_type=sa.String(67),
                type_=sa.String(64),
                existing_nullable=False,
            )
