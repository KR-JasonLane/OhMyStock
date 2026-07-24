import base64
import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, select, update as sql_update

from app.domain.notifications.models import OperationalEvent
from app.store.models import (Base, NotificationDeliveryRow,
                              NotificationOutboxRow,
                              TelegramCommandExecutionRow)
from app.store.notification_store import NotificationStore
from app.store.telegram_command_store import TelegramCommandStore
from app.store.telegram_inbox_store import TelegramInboxStore

NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)
OP = "v1:" + hashlib.sha256(b"op").hexdigest()
CHAT = "v1:" + hashlib.sha256(b"chat").hexdigest()


class Clock:
    value = NOW

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def stores(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'stores.db'}")
    Base.metadata.create_all(engine)
    clock = Clock()
    return (TelegramInboxStore(engine, now=clock),
            TelegramCommandStore(engine, now=clock),
            NotificationStore(engine, now=clock), clock)


def update(update_id, command="status", argument_hash=None):
    return {"update_id": update_id, "operator_hash": OP, "command": command,
            "argument_hash": argument_hash, "received_at": NOW}


def test_update와_outbox_key는_중복될_수_없다(stores):
    inbox, _, notifications, _ = stores
    inbox.persist_batch_and_offset([update(100)], 101)
    inbox.persist_batch_and_offset([update(100)], 101)
    assert inbox.count_updates() == 1
    notifications.enqueue_outbox("event:1:entry_filled", payload={})
    notifications.enqueue_outbox("event:1:entry_filled", payload={})
    assert notifications.count_outbox() == 1


def test_batch와_offset은_같은_트랜잭션이다(stores, monkeypatch):
    inbox, *_ = stores
    monkeypatch.setattr(inbox, "_insert_update", Mock(side_effect=RuntimeError))
    with pytest.raises(RuntimeError):
        inbox.persist_batch_and_offset([update(7)], 8)
    assert inbox.current_offset() == 0


def test_batch_중간실패는_앞선_update도_rollback한다(stores):
    inbox, *_ = stores
    with pytest.raises(ValueError):
        inbox.persist_batch_and_offset(
            [update(7), update(8, "confirm", "raw-token")], 9)
    assert inbox.count_updates() == 0
    assert inbox.current_offset() == 0


def test_offset은_후퇴하지_않는다(stores):
    inbox, *_ = stores
    inbox.persist_batch_and_offset([update(7)], 8)
    inbox.persist_batch_and_offset([], 3)
    assert inbox.current_offset() == 8


def test_expired_claim만_회수하고_terminal은_claim하지_않는다(stores):
    inbox, _, _, clock = stores
    inbox.seed_received(1)
    first = inbox.claim_next("worker-a", lease_s=10)
    assert inbox.claim_next("worker-b", lease_s=10) is None
    clock.advance(11)
    second = inbox.claim_next("worker-b", lease_s=10)
    assert second.update_id == first.update_id
    assert inbox.finish(1, "worker-a", first.version) is False
    assert inbox.finish(1, "worker-b", second.version)
    assert inbox.claim_next("worker-c") is None


def test_미허용폭주집계와_허용queue상한은_유계다(stores):
    inbox, *_ = stores
    subjects = [
        "v1:" + hashlib.sha256(str(i).encode()).hexdigest()
        for i in range(500)
    ]
    for start in range(0, 500, 100):
        inbox.persist_rejected_batch(NOW, subjects[start:start + 100], 300)
    assert inbox.rejected_counter_rows(NOW) == 300  # 298 subjects + total + overflow
    inbox.persist_rejected_batch(NOW, [OP] * 100, 300)
    assert inbox.rejected_counter_rows(NOW) == 300
    with pytest.raises(ValueError):
        inbox.persist_rejected_batch(NOW, [OP] * 101, 300)
    inbox.seed_allowed_updates(1000)
    assert inbox.can_poll(1000) is False


def _confirmation(commands, inbox, update_id=10):
    issued = commands.issue_confirmation(OP, CHAT, "resume", "fp", expires_in_s=999)
    digest = hashlib.sha256(issued.raw_token.encode()).hexdigest()
    inbox.persist_batch_and_offset([update(update_id, "confirm", digest)], update_id + 1)
    return issued, digest


def test_confirmation은_단일소비하고_실제_update에_intent를_원자생성한다(stores):
    inbox, commands, *_ = stores
    issued, digest = _confirmation(commands, inbox)
    first = commands.consume_and_create_intent(
        digest, OP, CHAT, "resume", "fp", NOW, update_id=10)
    second = commands.consume_and_create_intent(
        digest, OP, CHAT, "resume", "fp", NOW, update_id=10)
    assert first is not None and second is None
    assert commands.intent_count(issued.id) == 1
    with pytest.raises(ValueError):
        commands.consume_and_create_intent(
            digest, OP, CHAT, "resume", "fp", NOW, update_id=999)


def test_즉시명령은_update당_하나의_durable_intent만_만든다(stores):
    inbox, commands, *_ = stores
    inbox.persist_batch_and_offset([update(21, "stop")], 22)

    first = commands.create_intent_for_update(21, "stop")
    second = commands.create_intent_for_update(21, "stop")
    claimed = commands.claim_intent_by_id(first.id, "worker")

    assert first.id == second.id == "telegram_command_update_21"
    assert claimed is not None and claimed.id == first.id
    assert commands.claim_intent("other") is None


def test_confirmation_CSPRNG_2분귀속과_이전토큰무효화(stores):
    inbox, commands, *_ = stores
    first = commands.issue_confirmation(OP, CHAT, "resume", "fp")
    second = commands.issue_confirmation(OP, CHAT, "resume", "fp")
    assert len(base64.urlsafe_b64decode(first.raw_token + "==")) >= 24

    def consume(raw, operator=OP, chat=CHAT, command="resume",
                fingerprint="fp", now=NOW, uid=11):
        digest = hashlib.sha256(raw.encode()).hexdigest()
        inbox.persist_batch_and_offset([update(uid, "confirm", digest)], uid + 1)
        return commands.consume_and_create_intent(
            digest, operator, chat, command, fingerprint, now, update_id=uid)

    assert consume(first.raw_token) is None
    assert consume(second.raw_token, "v1:" + "f" * 64) is None
    assert consume(second.raw_token, OP, "v1:" + "e" * 64) is None
    assert consume(second.raw_token, OP, CHAT, "stop") is None
    assert consume(second.raw_token, OP, CHAT, "resume", "changed") is None
    assert consume(second.raw_token, now=NOW + timedelta(seconds=121)) is None


def test_running은_재실행하지_않고_unknown_reconciliation으로만_간다(stores):
    inbox, commands, _, clock = stores
    _, digest = _confirmation(commands, inbox)
    intent = commands.consume_and_create_intent(
        digest, OP, CHAT, "resume", "fp", NOW, update_id=10)
    claimed = commands.claim_intent("w", lease_s=10)
    assert commands.mark_running(intent.id, "w", claimed.version)
    clock.advance(11)
    assert commands.claim_intent("other") is None
    assert commands.expire_running_to_unknown() == 1
    reconciled = commands.claim_reconciliation("reconciler")
    assert reconciled.id == intent.id


def test_reconciliation_crash는_lease후_다른worker가_회수한다(stores):
    inbox, commands, _, clock = stores
    _, digest = _confirmation(commands, inbox)
    intent = commands.consume_and_create_intent(
        digest, OP, CHAT, "resume", "fp", NOW, update_id=10)
    claimed = commands.claim_intent("runner", lease_s=1)
    assert commands.mark_running(intent.id, "runner", claimed.version)
    clock.advance(2)
    commands.expire_running_to_unknown()
    first = commands.claim_reconciliation("r1", lease_s=10)
    clock.advance(11)
    second = commands.claim_reconciliation("r2", lease_s=10)
    assert second.id == first.id
    assert not commands.mark_terminal(
        first.id, "r1", first.version, "needs_attention")
    assert commands.mark_terminal(
        second.id, "r2", second.version, "needs_attention")


def test_부분성공_뒤_미전송_chunk만_claim하고_완료시_purge(stores):
    _, _, notifications, _ = stores
    oid = notifications.enqueue_parts("k", ["1", "2", "3"], sensitive=True)
    claimed = notifications.claim_deliveries("w")
    assert notifications.finish_delivery(
        claimed[0].id, "w", claimed[0].version, telegram_message_id=99)
    assert notifications.load_payload(oid) is None
    assert notifications.load_delivery_bodies(oid) == [None, "2", "3"]
    remaining = notifications.claim_deliveries("w2")
    assert [item.part_index for item in remaining] == [2]
    for item in remaining:
        assert notifications.finish_delivery(
            item.id, "w2", item.version, telegram_message_id=100 + item.part_index)
    final = notifications.claim_deliveries("w3")
    assert [item.part_index for item in final] == [3]
    assert notifications.finish_delivery(
        final[0].id, "w3", final[0].version, telegram_message_id=103)
    assert notifications.load_payload(oid) is None
    assert notifications.load_delivery_bodies(oid) == [None, None, None]


def test_delivery_stale_worker와_조각별_retry예산(stores):
    _, _, notifications, _ = stores
    oid = notifications.enqueue_parts("parts", ["1", "2"])
    first = notifications.claim_deliveries("w")[0]
    assert notifications.retry_delivery(first.id, "w", first.version, "timeout", None)
    assert not notifications.finish_delivery(
        first.id, "w", first.version, telegram_message_id=9)
    rows = notifications.load_deliveries(oid)
    assert rows[0].attempt_count == 1 and rows[1].attempt_count == 0


def test_delivery_dead_letter는_lease_fence와_부모종결을_원자보장한다(stores):
    _, _, notifications, _ = stores
    oid = notifications.enqueue_parts("dead-fence", ["one", "two"])
    first = notifications.claim_deliveries("w")[0]

    assert not notifications.dead_letter_delivery(
        first.id, "other", first.version, "http_400", 400)
    assert notifications.dead_letter_delivery(
        first.id, "w", first.version, "http_400", 400)
    assert [row.status for row in notifications.load_deliveries(oid)] == [
        "dead_letter", "dead_letter"]


def test_같은_outbox는_다음조각하나만_lease해_영구실패와_경합하지않는다(stores):
    _, _, notifications, _ = stores
    oid = notifications.enqueue_parts("serialized", ["one", "two"])

    first = notifications.claim_deliveries("first", limit=20, lease_s=60)
    assert [row.part_index for row in first] == [1]
    assert notifications.claim_deliveries("second", limit=20, lease_s=60) == []
    assert notifications.finish_delivery(
        first[0].id, "first", first[0].version, telegram_message_id=1)
    second = notifications.claim_deliveries("second", limit=20, lease_s=60)
    assert [row.part_index for row in second] == [2]


def test_backoff중_앞조각이_다른_outbox의_긴급claim을_막지않는다(stores):
    _, _, notifications, clock = stores
    blocked = notifications.enqueue_parts("blocked", ["one", "two"], priority=0)
    first = notifications.claim_deliveries("first", limit=1)[0]
    assert notifications.retry_delivery(
        first.id, "first", first.version, "timeout", None,
        next_attempt_at=clock.value + timedelta(minutes=1))
    ready = notifications.enqueue_parts("ready", ["stop-loss"], priority=0)

    claimed = notifications.claim_deliveries("second", limit=1)
    assert [row.outbox_id for row in claimed] == [ready]
    assert claimed[0].outbox_id != blocked


def test_delivery는_priority_occurred_part순으로_claim한다(stores):
    _, _, notifications, _ = stores
    notifications.enqueue_parts(
        "normal", ["n"], priority=10, occurred_at=NOW - timedelta(minutes=1))
    notifications.enqueue_parts(
        "critical", ["c1", "c2"], priority=0, occurred_at=NOW)
    claimed = notifications.claim_deliveries("w")
    assert [item.body for item in claimed] == ["c1", "n"]
    critical = claimed[0]
    assert notifications.finish_delivery(
        critical.id, "w", critical.version, telegram_message_id=1)
    next_critical = notifications.claim_deliveries("w")
    assert [item.body for item in next_critical] == ["c2"]


def test_서로다른원천의_같은숫자버전은_충돌하지않는다(stores):
    _, _, notifications, _ = stores
    notifications.append_event(OperationalEvent(
        "entry_filled", "trade_order", 7, "1", {}, NOW))
    notifications.append_event(OperationalEvent(
        "pipeline_gave_up", "scheduler_event", 7, "1", {}, NOW))
    assert notifications.operational_event_count() == 2


def test_append_event는_호출자_transaction에_참여한다(stores):
    _, _, notifications, _ = stores
    with pytest.raises(RuntimeError), notifications._sessions.begin() as session:
        notifications.append_event_in_session(session, OperationalEvent(
            "entry_filled", "trade_order", 8, "1", {}, NOW))
        raise RuntimeError
    assert notifications.operational_event_count() == 0


def test_sensitive_deadline과_빈parts_검증(stores):
    _, _, notifications, _ = stores
    with pytest.raises(ValueError):
        notifications.enqueue_parts("empty", [])
    oid = notifications.enqueue_outbox(
        "query", {"amount": 1}, sensitive=True, retention_kind="query")
    # enqueue_outbox without rendered parts still purges payload and metadata survives.
    assert notifications.purge_expired_sensitive(NOW + timedelta(minutes=16)) == 1
    assert notifications.load_payload(oid) is None


def test_sensitive_TTL은_활성sender를_기다리고_deadletter본문도_purge한다(stores):
    _, _, notifications, _ = stores
    oid = notifications.enqueue_parts(
        "ttl", ["민감"], sensitive=True, retention_kind="query")
    claimed = notifications.claim_deliveries("sender", lease_s=2000)
    assert notifications.purge_expired_sensitive(
        NOW + timedelta(minutes=16)) == 0
    assert notifications.load_delivery_bodies(oid) == ["민감"]
    assert notifications.purge_expired_sensitive(
        NOW + timedelta(minutes=34)) == 1
    assert notifications.load_delivery_bodies(oid) == [None]
    assert not notifications.finish_delivery(
        claimed[0].id, "sender", claimed[0].version, telegram_message_id=1)

    dead = notifications.enqueue_parts(
        "dead", ["dead body"], sensitive=True, retention_kind="query")
    with notifications._sessions.begin() as session:
        session.execute(sql_update(NotificationOutboxRow).where(
            NotificationOutboxRow.id == dead).values(status="dead_letter"))
        session.execute(sql_update(NotificationDeliveryRow).where(
            NotificationDeliveryRow.outbox_id == dead).values(status="dead_letter"))
    assert notifications.purge_expired_sensitive(
        NOW + timedelta(minutes=16)) == 1
    assert notifications.load_delivery_bodies(dead) == [None]


def test_delivery_조각수_문자수_총byte상한(stores):
    _, _, notifications, _ = stores
    notifications.enqueue_parts("unicode", ["가" * 4096])
    with pytest.raises(ValueError):
        notifications.enqueue_parts("chars", ["가" * 4097])
    with pytest.raises(ValueError):
        notifications.enqueue_parts("many", ["x"] * 65)
    with pytest.raises(ValueError):
        notifications.enqueue_parts("total", ["가" * 4096] * 64)


def test_bounded_retention은_미종결과_unknown을_지우지_않는다(stores):
    inbox, commands, notifications, clock = stores
    inbox.seed_received(1)
    claim = inbox.claim_next("w")
    assert inbox.finish(1, "w", claim.version)
    inbox.seed_received(2)
    assert inbox.purge_terminal_before(NOW + timedelta(days=31), limit=1) == 1
    assert inbox.count_updates() == 1

    _, digest = _confirmation(commands, inbox, update_id=10)
    intent = commands.consume_and_create_intent(
        digest, OP, CHAT, "resume", "fp", NOW, update_id=10)
    claimed = commands.claim_intent("w", lease_s=1)
    assert commands.mark_running(intent.id, "w", claimed.version)
    clock.advance(2)
    commands.expire_running_to_unknown()
    # confirmation linked to unknown intent is audit evidence and is retained.
    assert commands.purge_confirmations_before(
        NOW + timedelta(days=91), limit=1) == 0

    pending = notifications.enqueue_outbox("pending", {})
    sent = notifications.enqueue_parts("sent", ["x"])
    delivery = notifications.claim_deliveries("sender")[0]
    # priority/order may claim the pending-less outbox's only actual delivery.
    assert delivery.outbox_id == sent
    assert notifications.finish_delivery(
        delivery.id, "sender", delivery.version, telegram_message_id=1)
    assert notifications.purge_retention(
        NOW + timedelta(days=366), limit=1) == 1
    assert notifications.load_payload(pending) == {}


def test_terminal_execution_confirmation은_unlink후_90일삭제한다(stores):
    inbox, commands, *_ = stores
    issued, digest = _confirmation(commands, inbox)
    intent = commands.consume_and_create_intent(
        digest, OP, CHAT, "resume", "fp", NOW, update_id=10)
    claimed = commands.claim_intent("w")
    assert commands.mark_running(intent.id, "w", claimed.version)
    assert commands.mark_terminal(
        intent.id, "w", claimed.version + 1, "succeeded")
    assert commands.purge_confirmations_before(
        NOW + timedelta(days=91), limit=1) == 1
    with commands._sessions() as session:
        row = session.scalar(select(TelegramCommandExecutionRow).where(
            TelegramCommandExecutionRow.id == intent.id))
        assert row.confirmation_id is None


def test_raw_token과_naive_datetime은_저장경계에서_거부(stores):
    inbox, *_ = stores
    with pytest.raises(ValueError):
        inbox.persist_batch_and_offset([update(1, "confirm", "raw-token")], 2)
    with pytest.raises(ValueError):
        inbox.persist_rejected_batch(datetime(2026, 7, 24), [OP], 1)
    for bad in (True, "1", -1):
        with pytest.raises(ValueError):
            inbox.persist_batch_and_offset([], bad)
    with pytest.raises(ValueError):
        inbox.persist_batch_and_offset([
            {**update(2), "correlation_id": "bad:identifier"}], 3)
