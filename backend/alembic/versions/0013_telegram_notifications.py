"""Telegram durable inbox, commands, operational events and notification outbox."""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("telegram_state",
        sa.Column("key", sa.String(32), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("lease_owner", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("telegram_updates",
        sa.Column("update_id", sa.BigInteger(), primary_key=True),
        sa.Column("operator_hash", sa.String(64), nullable=False),
        sa.Column("command", sa.String(24), nullable=False),
        sa.Column("argument_hash", sa.String(64)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("owner", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)))
    op.create_table("telegram_confirmations",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("operator_hash", sa.String(64), nullable=False),
        sa.Column("chat_hash", sa.String(64), nullable=False),
        sa.Column("command", sa.String(24), nullable=False),
        sa.Column("state_fingerprint", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("telegram_confirmation_locks",
        sa.Column("operator_hash", sa.String(64), primary_key=True))
    op.create_table("telegram_command_executions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("update_id", sa.BigInteger(), sa.ForeignKey("telegram_updates.update_id"), nullable=False),
        sa.Column("confirmation_id", sa.BigInteger(), sa.ForeignKey("telegram_confirmations.id")),
        sa.Column("command", sa.String(24), nullable=False),
        sa.Column("state_fingerprint", sa.String(128), nullable=False),
        sa.Column("targets_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("owner", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_kind", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)))
    op.create_table("telegram_command_audit",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("intent_id", sa.String(64)),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("result", sa.String(24), nullable=False),
        sa.Column("error_kind", sa.String(64)),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False))
    op.create_table("telegram_rejected_update_counters",
        sa.Column("minute", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("subject_hash", sa.String(64), primary_key=True),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"))
    op.create_table("operational_events",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("source_version", sa.String(96), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_type", "source_id", "source_version",
                            name="uq_operational_event_source_version"))
    op.create_table("notification_outbox",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("sensitive", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("payload", sa.Text()),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_kind", sa.String(64)),
        sa.Column("retention_kind", sa.String(24), nullable=False, server_default="standard"),
        sa.Column("purge_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("idempotency_key", name="uq_notification_outbox_key"))
    op.create_table("notification_deliveries",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("outbox_id", sa.BigInteger(), sa.ForeignKey("notification_outbox.id"), nullable=False),
        sa.Column("part_index", sa.Integer(), nullable=False),
        sa.Column("total_parts", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text()),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("owner", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_kind", sa.String(64)),
        sa.Column("last_http_status", sa.Integer()),
        sa.Column("telegram_message_id", sa.BigInteger()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("outbox_id", "part_index", name="uq_notification_delivery_part"))
    op.create_index("ix_notification_delivery_claim", "notification_deliveries",
                    ["status", "next_attempt_at", "lease_until", "id"])
    op.create_index("ix_telegram_updates_claim", "telegram_updates",
                    ["status", "lease_until", "update_id"])
    op.create_index("ix_telegram_updates_retention", "telegram_updates",
                    ["status", "finished_at", "update_id"])
    op.create_index("ix_telegram_confirmations_retention", "telegram_confirmations",
                    ["consumed_at", "expires_at", "id"])
    op.create_index("ix_telegram_command_audit_retention", "telegram_command_audit",
                    ["ts", "id"])
    op.create_index("ix_telegram_rejected_retention",
                    "telegram_rejected_update_counters",
                    ["minute", "subject_hash"])
    op.create_index("ix_telegram_executions_claim", "telegram_command_executions",
                    ["status", "lease_until", "created_at"])
    op.create_index("ix_notification_outbox_retention", "notification_outbox",
                    ["status", "sent_at", "id"])
    op.create_index("ix_notification_outbox_purge", "notification_outbox",
                    ["purge_at", "status", "id"])


def downgrade() -> None:
    op.drop_index("ix_notification_outbox_purge", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_retention", table_name="notification_outbox")
    op.drop_index("ix_telegram_executions_claim", table_name="telegram_command_executions")
    op.drop_index("ix_telegram_updates_claim", table_name="telegram_updates")
    op.drop_index("ix_telegram_updates_retention", table_name="telegram_updates")
    op.drop_index("ix_telegram_confirmations_retention", table_name="telegram_confirmations")
    op.drop_index("ix_telegram_command_audit_retention", table_name="telegram_command_audit")
    op.drop_index("ix_telegram_rejected_retention",
                  table_name="telegram_rejected_update_counters")
    op.drop_index("ix_notification_delivery_claim", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")
    op.drop_table("notification_outbox")
    op.drop_table("operational_events")
    op.drop_table("telegram_rejected_update_counters")
    op.drop_table("telegram_command_audit")
    op.drop_table("telegram_command_executions")
    op.drop_table("telegram_confirmation_locks")
    op.drop_table("telegram_confirmations")
    op.drop_table("telegram_updates")
    op.drop_table("telegram_state")
