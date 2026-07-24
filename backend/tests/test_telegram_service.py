import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import re

import pytest
from sqlalchemy import create_engine, select

from app.adapters.telegram import (TelegramAuthenticationError,
                                   TelegramPermanentError,
                                   TelegramRateLimited,
                                   TelegramTemporaryError)
from app.core.telegram_service import (DigestService, Maintenance, OutboxSender,
                                       TelegramCircuit)
from app.domain.notifications.digest import Digest, DigestAccount, DigestSection
from app.domain.notifications.models import NotificationPriority
from app.domain.notifications.models import OperationalEvent
from app.domain.notifications.projector import NotificationProjector
from app.store.models import Base, NotificationDeliveryRow, NotificationOutboxRow
from app.store.notification_store import NotificationStore


NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)


class Clock:
    value = NOW

    def __call__(self):
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class FakeTelegram:
    def __init__(self) -> None:
        self._failures: list[BaseException] = []
        self._part_failures: dict[str, BaseException] = {}
        self.messages: list[str] = []
        self.sent_part_counts: Counter[str] = Counter()

    def fail(self, exc: BaseException) -> None:
        self._failures.append(exc)

    def fail_part_once(self, part: str, exc: BaseException) -> None:
        self._part_failures[part] = exc

    async def send_message(self, chat_id: int, text: str) -> int:
        assert chat_id == 1234
        self.messages.append(text)
        part = re.search(r"\[part-\d\]", text).group(0) if "[part-" in text else text
        self.sent_part_counts[part] += 1
        if part in self._part_failures:
            raise self._part_failures.pop(part)
        if self._failures:
            raise self._failures.pop(0)
        return len(self.messages)


@pytest.fixture
def sender(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'sender.db'}")
    Base.metadata.create_all(engine)
    clock = Clock()
    store = NotificationStore(engine, now=clock)
    telegram = FakeTelegram()
    return OutboxSender(
        store, telegram, chat_id=1234, worker_id="sender-test", now=clock,
        random_float=lambda: 0.0), store, telegram, clock


def _state(store, outbox_id: int):
    with store._sessions() as session:
        outbox = session.get(NotificationOutboxRow, outbox_id)
        delivery = session.scalar(select(NotificationDeliveryRow).where(
            NotificationDeliveryRow.outbox_id == outbox_id))
        return outbox, delivery


def _utc(value):
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@pytest.mark.anyio
async def test_429는_retry_after를_따르고_lease_fence로_재시도한다(sender):
    worker, store, telegram, clock = sender
    oid = store.enqueue_parts("rate", ["one"])
    telegram.fail(TelegramRateLimited("sendMessage", 17))

    assert await worker.run_once() == 1
    outbox, delivery = _state(store, oid)
    assert outbox.status == "pending"
    assert delivery.status == "pending"
    assert _utc(delivery.next_attempt_at) == clock.value + timedelta(seconds=17)
    assert delivery.attempt_count == 1
    assert delivery.last_http_status == 429


@pytest.mark.anyio
async def test_operational_event는_projector_원자outbox를거쳐_sender가_전송한다(sender):
    worker, store, telegram, clock = sender
    store.append_event(OperationalEvent(
        "entry_filled", "trade_order", 7, "1", {"quantity": 3}, clock.value))

    assert NotificationProjector(store).project_batch() == 1
    assert await worker.run_once() == 1
    assert telegram.messages[0].startswith("[operational-1] [1/1]\n알림: entry_filled")


@pytest.mark.anyio
async def test_projector는_과거_zero_child_operational_outbox도_복구한다(sender):
    worker, store, telegram, _ = sender
    store.enqueue_outbox(
        "operational:7:entry_filled",
        {"operational_event_id": 7, "event_kind": "entry_filled",
         "notification_kind": "entry_filled", "source_type": "trade_order",
         "source_id": 7, "source_version": "1", "facts": {"symbol": "005930"}},
        kind="entry_filled")

    assert NotificationProjector(store).project_batch() == 0
    assert await worker.run_once() == 1
    assert telegram.messages[0].startswith("[operational-7] [1/1]")


@pytest.mark.anyio
async def test_projector는_민감키가든_원천facts를_telegram으로_렌더하지않는다(sender):
    worker, store, telegram, clock = sender
    store.append_event(OperationalEvent(
        "entry_filled", "trade_order", 8, "1",
        {"symbol": "005930", "token": "TOPSECRET", "account_number": "123"}, clock.value))

    NotificationProjector(store).project_batch()
    assert await worker.run_once() == 1
    assert "TOPSECRET" not in telegram.messages[0]
    assert "account_number" not in telegram.messages[0]
    assert "005930" in telegram.messages[0]


@pytest.mark.anyio
async def test_projector는_알수없는_notification_kind를_외부전송하지않는다(sender):
    worker, store, telegram, clock = sender
    store.append_event(OperationalEvent(
        "entry_filled", "trade_order", 9, "1",
        {"notification_kinds": ["TOPSECRET"], "symbol": "005930"}, clock.value))

    projector = NotificationProjector(store)
    assert projector.project_batch() == 0
    assert projector.checkpoint() == 1
    store.append_event(OperationalEvent(
        "scheduler_dead", "scheduler_service", 0, "2",
        {"reason": "restart_budget_exhausted"}, clock.value))
    assert projector.project_batch() == 1
    assert await worker.run_once() == 1
    assert "scheduler_dead" in telegram.messages[0]


@pytest.mark.anyio
async def test_trading_monitoring_gap은_원인과_run을_보존해_전송한다(sender):
    worker, store, telegram, clock = sender
    store.append_event(OperationalEvent(
        "pipeline_gave_up", "scheduler_event", 3, "1",
        {"job": "trade", "reason": "retry_exhausted", "run_id": 22,
         "notification_kinds": ["trading_monitoring_gap"]}, clock.value))

    NotificationProjector(store).project_batch()
    assert await worker.run_once() == 1
    assert '"job":"trade"' in telegram.messages[0]
    assert '"reason":"retry_exhausted"' in telegram.messages[0]


@pytest.mark.anyio
async def test_영구4xx는_즉시_부모와_미전송조각을_dead_letter한다(sender):
    worker, store, telegram, _ = sender
    oid = store.enqueue_parts("bad", ["first", "second"])
    telegram.fail(TelegramPermanentError("sendMessage", "http_400"))

    assert await worker.run_once() == 1
    outbox, _ = _state(store, oid)
    assert outbox.status == "dead_letter"
    assert [row.status for row in store.load_deliveries(oid)] == [
        "dead_letter", "dead_letter"]


@pytest.mark.anyio
async def test_5xx는_지수backoff로_예약하고_401은_sender를_dead로_연다(sender):
    worker, store, telegram, clock = sender
    retry = store.enqueue_parts("retry", ["one"])
    telegram.fail(TelegramTemporaryError("sendMessage", "server"))
    assert await worker.run_once() == 1
    _, delivery = _state(store, retry)
    assert _utc(delivery.next_attempt_at) == clock.value + timedelta(seconds=1)
    with store._sessions.begin() as session:
        session.execute(NotificationDeliveryRow.__table__.update().where(
            NotificationDeliveryRow.outbox_id == retry).values(
                next_attempt_at=clock.value + timedelta(hours=1)))

    auth = store.enqueue_parts("auth", ["two"])
    clock.advance(2)
    telegram.fail(TelegramAuthenticationError("sendMessage"))
    await worker.run_once()
    assert worker.snapshot()["state"] == "dead"
    _, delivery = _state(store, auth)
    assert delivery.status == "sending"


@pytest.mark.anyio
async def test_5분지난긴급알림은_지연표시하고_성공chunk를_다시안보낸다(sender):
    worker, store, telegram, clock = sender
    oid = store.enqueue_parts(
        "delayed", ["[part-1]\none", "[part-2]\ntwo", "[part-3]\nthree"],
        sensitive=True, priority=NotificationPriority.CRITICAL,
        occurred_at=clock.value - timedelta(minutes=6))
    telegram.fail_part_once("[part-2]", TelegramTemporaryError("sendMessage", "network"))

    assert await worker.run_once() == 1
    assert await worker.run_once() == 1
    assert await worker.run_once() == 0
    clock.advance(1)
    assert await worker.run_once() == 1
    assert await worker.run_once() == 1
    assert telegram.messages[0].startswith(
        "[지연 알림] 발생 시각: 2026-07-24T08:54:00+09:00")
    assert telegram.sent_part_counts == {"[part-1]": 1, "[part-2]": 2, "[part-3]": 1}
    assert store.load_payload(oid) is None
    assert store.load_delivery_bodies(oid) == [None, None, None]


@pytest.mark.anyio
async def test_지연표지공간을_넘는긴급본문은_enqueue에서_거부한다(sender):
    worker, store, telegram, clock = sender
    with pytest.raises(ValueError, match="critical"):
        store.enqueue_parts(
            "long-delayed", ["x" * 4096], priority=NotificationPriority.CRITICAL,
            occurred_at=clock.value - timedelta(minutes=6))
    assert await worker.run_once() == 0
    assert telegram.messages == []


@pytest.mark.anyio
async def test_인증실패는_주입된공유회로를_dead로_전파한다(sender):
    _, store, telegram, _ = sender
    circuit = TelegramCircuit()
    worker = OutboxSender(
        store, telegram, chat_id=1234, worker_id="shared-circuit",
        authentication_circuit=circuit)
    store.enqueue_parts("auth-circuit", ["one"])
    telegram.fail(TelegramAuthenticationError("sendMessage"))

    await worker.run_once()
    assert circuit.snapshot() == {"state": "dead", "reason": "authentication_failed"}


@pytest.mark.anyio
async def test_지연표지와본문의_두_http호출보다긴_lease를_claim한다(sender):
    _, store, _, clock = sender
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingTelegram:
        async def send_message(self, chat_id: int, text: str) -> int:
            started.set()
            await release.wait()
            return 1

    worker = OutboxSender(
        store, BlockingTelegram(), chat_id=1234, worker_id="long-lease", now=clock)
    oid = store.enqueue_parts(
        "double-send", ["x" * 4032], priority=NotificationPriority.CRITICAL,
        occurred_at=clock.value - timedelta(minutes=6))

    task = asyncio.create_task(worker.run_once())
    await started.wait()
    _, delivery = _state(store, oid)
    assert _utc(delivery.lease_until) == clock.value + timedelta(seconds=90)
    release.set()
    await task


@pytest.mark.anyio
async def test_조각별_10회시도와_24시간_age는_전송전_dead_letter한다(sender):
    worker, store, telegram, clock = sender
    oid = store.enqueue_parts("budget", ["one"])
    with store._sessions.begin() as session:
        session.execute(
            NotificationDeliveryRow.__table__.update().where(
                NotificationDeliveryRow.outbox_id == oid).values(attempt_count=10))
    assert await worker.run_once() == 1
    assert _state(store, oid)[0].status == "dead_letter"
    assert telegram.messages == []

    stale = store.enqueue_parts("stale", ["two"])
    with store._sessions.begin() as session:
        session.execute(NotificationOutboxRow.__table__.update().where(
            NotificationOutboxRow.id == stale).values(
                created_at=clock.value - timedelta(hours=25)))
    assert await worker.run_once() == 1
    assert _state(store, stale)[0].status == "dead_letter"


@pytest.mark.anyio
async def test_maintenance는_민감만료와_보존기간을_엄격한_batch로_정리한다(sender):
    _, store, _, clock = sender
    account_id = store.enqueue_parts(
        "account-ttl", ["예수금 1000000"], sensitive=True, retention_kind="query",
        priority=NotificationPriority.DIGEST)
    digest_id = store.enqueue_parts(
        "digest-ttl", ["총평가 1200000"], sensitive=True, retention_kind="digest",
        priority=NotificationPriority.DIGEST)
    for index in range(500):
        outbox_id = store.enqueue_parts(f"sent-{index}", ["metadata"])
        delivery = store.claim_deliveries("seed")[0]
        assert delivery.outbox_id == outbox_id
        assert store.finish_delivery(
            delivery.id, "seed", delivery.version, telegram_message_id=index)
    with store._sessions.begin() as session:
        session.execute(NotificationOutboxRow.__table__.update().where(
            NotificationOutboxRow.status == "sent"
        ).values(sent_at=NOW - timedelta(days=366)))

    clock.advance(25 * 60 * 60)
    deleted = await Maintenance(store, now=clock, batch_size=500).run_once()

    assert deleted == 500
    assert store.load_payload(account_id) is None
    assert store.load_delivery_bodies(account_id) == [None]
    assert store.load_payload(digest_id) is None
    assert store.load_delivery_bodies(digest_id) == [None]
    assert _state(store, account_id)[0].status == "dead_letter"


@pytest.mark.anyio
async def test_digest_service는_builder결과를_thread의_원자outbox로_한번만_materialize한다(sender):
    _, store, _, clock = sender

    class Planner:
        marked = []

        def due_dates(self, now):
            return (datetime(2026, 7, 24, tzinfo=timezone.utc).date(),)

        def mark_generated(self, trading_day):
            self.marked.append(trading_day)

    class Builder:
        async def build(self, trading_day):
            return Digest(
                trading_day, "mock",
                DigestSection({"collection_status": "done"}, clock()),
                DigestSection({"order_count": 1}, clock()),
                DigestAccount(None, None, None, None, "unknown", "unavailable",
                              ("account_snapshot",), None, None))

    planner = Planner()
    service = DigestService(planner, Builder(), store, now=clock)

    assert await service.run_once() == 1
    assert await service.run_once() == 0
    assert planner.marked == [datetime(2026, 7, 24, tzinfo=timezone.utc).date()]
    assert store.count_outbox() == 1


@pytest.mark.anyio
async def test_sender는_dead상태에서도_만료본문을_scrub하고_35초이내만료건을_claim하지않는다(sender):
    _, store, telegram, clock = sender
    expired = store.enqueue_parts(
        "expired-query", ["예수금 1000000"], sensitive=True, retention_kind="query")
    clock.advance(16 * 60)
    dead_circuit = TelegramCircuit()
    dead_circuit.mark_dead("authentication_failed")
    dead = OutboxSender(store, telegram, chat_id=1234, worker_id="dead", now=clock,
                         authentication_circuit=dead_circuit)
    assert await dead.run_once() == 0
    assert store.load_payload(expired) is None
    assert store.load_delivery_bodies(expired) == [None]

    near_expiry = store.enqueue_parts(
        "near-expiry", ["총평가 1200000"], sensitive=True, retention_kind="query")
    with store._sessions.begin() as session:
        session.execute(NotificationOutboxRow.__table__.update().where(
            NotificationOutboxRow.id == near_expiry
        ).values(purge_at=clock.value + timedelta(seconds=30)))
    active = OutboxSender(store, telegram, chat_id=1234, worker_id="active", now=clock)
    assert await active.run_once() == 0
    assert telegram.messages == []


@pytest.mark.anyio
async def test_sender는_전체전송_deadline을_넘기면_재시도한다(sender):
    _, store, _, clock = sender

    class BlockingTelegram:
        async def send_message(self, chat_id, text):
            await asyncio.Event().wait()

    worker = OutboxSender(store, BlockingTelegram(), chat_id=1234,
                          worker_id="deadline", now=clock)
    worker._SEND_DEADLINE_S = 0.01
    outbox_id = store.enqueue_parts("deadline", ["one"])

    assert await worker.run_once() == 1
    _, delivery = _state(store, outbox_id)
    assert delivery.status == "pending"
    assert delivery.last_error_kind == "send_deadline"


def test_sender는_TTL_guard가_전체전송_deadline보다길어야한다(sender, monkeypatch):
    _, store, telegram, _ = sender
    monkeypatch.setattr(NotificationStore, "SENSITIVE_DELIVERY_MIN_TTL_S", 30)

    with pytest.raises(ValueError, match="TTL guard"):
        OutboxSender(store, telegram, chat_id=1234, worker_id="guard")
