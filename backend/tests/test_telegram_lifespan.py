import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine

from app.core.config import Settings
from app.core.telegram_service import (
    CommandDispatcher,
    CommandResponsePublisher,
    CompositeSender,
    EphemeralResponseSender,
    InboxPoller,
    TelegramCircuit,
    TelegramService,
    external_id_hash,
)
from app.adapters.telegram.client import TelegramClient, TelegramPermanentError
from app.domain.notifications.commands import CommandProcessor, CommandResult
from app.domain.notifications.models import InboundMessage, OperatorIdentity
from app.store.models import Base
from app.store.telegram_inbox_store import TelegramInboxStore
from app.store.telegram_command_store import TelegramCommandStore
from app.store.notification_store import NotificationStore


NOW = datetime(2026, 7, 24, 1, 2, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class FakeTelegram:
    def __init__(self, updates=()) -> None:
        self.updates = list(updates)
        self.calls = 0

    async def get_updates(self, offset: int):
        self.calls += 1
        return [item for item in self.updates if item.update_id >= offset]


def _store(tmp_path, clock: Clock) -> TelegramInboxStore:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'telegram.db'}")
    Base.metadata.create_all(engine)
    return TelegramInboxStore(engine, now=clock)


def _message(update_id: int, text: str, *, allowed: bool = True) -> InboundMessage:
    return InboundMessage(
        update_id=update_id,
        user_id=11 if allowed else 9000 + update_id,
        chat_id=22 if allowed else 8000 + update_id,
        chat_type="private",
        text=text,
        forwarded=False,
    )


def test_외부식별자는_v1_HMAC만_남긴다():
    token = SecretStr("BOT-TOKEN")

    digest = external_id_hash(token, "user", 11)

    assert digest.startswith("v1:")
    assert len(digest) == 67
    assert digest != "11"
    assert external_id_hash(token, "chat", 11) != digest


@pytest.mark.anyio
async def test_poller는_단일lease와_원자offset을_지킨다(tmp_path, monkeypatch):
    clock = Clock()
    store = _store(tmp_path, clock)
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingTelegram(FakeTelegram):
        async def get_updates(self, offset: int):
            self.calls += 1
            entered.set()
            await release.wait()
            return [_message(1, "/stop")]

    telegram = BlockingTelegram()
    kwargs = dict(
        telegram=telegram,
        inbox=store,
        operators=(OperatorIdentity(11, 22),),
        bot_token=SecretStr("BOT-TOKEN"),
        circuit=TelegramCircuit(),
        now=clock,
    )
    first = InboxPoller(worker_id="poller-a", **kwargs)
    second = InboxPoller(worker_id="poller-b", **kwargs)

    task = asyncio.create_task(first.run_once())
    await entered.wait()
    assert await second.run_once() == 0
    assert telegram.calls == 1
    release.set()
    assert await task == 1
    assert store.current_offset() == 2
    assert store.count_updates() == 1

    original = store._persist_rejected_in_session

    def fail_counter(*args, **kwargs):
        raise RuntimeError("db-write")

    monkeypatch.setattr(store, "_persist_rejected_in_session", fail_counter)
    telegram.updates = [_message(2, "/stop"), _message(3, "/help", allowed=False)]
    with pytest.raises(RuntimeError, match="db-write"):
        await first.run_once()
    assert store.current_offset() == 2
    assert store.count_updates() == 1
    monkeypatch.setattr(store, "_persist_rejected_in_session", original)


@pytest.mark.anyio
async def test_poller는_미허용폭주를_bounded집계하고_1000상한에서_backoff(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    telegram = FakeTelegram()
    poller = InboxPoller(
        telegram=telegram,
        inbox=store,
        operators=(OperatorIdentity(11, 22),),
        bot_token=SecretStr("BOT-TOKEN"),
        worker_id="poller",
        circuit=TelegramCircuit(),
        now=clock,
    )

    for page in range(5):
        telegram.updates = [
            _message(page * 100 + index, "/help", allowed=False)
            for index in range(100)
        ]
        assert await poller.run_once() == 100

    minute = clock().replace(second=0, microsecond=0)
    assert store.rejected_counter_rows(minute) <= 300
    assert store.rejected_total(minute) == 500

    store.seed_allowed_updates(1000)
    calls = telegram.calls
    assert await poller.run_once() == 0
    assert telegram.calls == calls
    assert poller.snapshot()["backoff_reason"] == "allowed_queue_full"


class RecordingProcessor:
    def __init__(self) -> None:
        self.seen: list[tuple[int, str]] = []
        self.shutdown_calls = 0

    async def process_claimed(self, claimed) -> CommandResult:
        self.seen.append((claimed.update_id, claimed.command))
        return CommandResult(claimed.command)

    async def reconcile_unknown(self):
        return None

    async def shutdown_accepted_monitors(self) -> None:
        self.shutdown_calls += 1


@pytest.mark.anyio
async def test_제어lane은_update순서를_지키고_느린조회와_분리된다(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    operator_hash = "v1:" + "a" * 64
    store.persist_batch_and_offset(
        [
            {"update_id": 10, "operator_hash": operator_hash,
             "command": "account", "received_at": clock()},
            {"update_id": 11, "operator_hash": operator_hash,
             "command": "stop", "received_at": clock()},
            {"update_id": 12, "operator_hash": operator_hash,
             "command": "pause", "received_at": clock()},
        ],
        13,
    )
    processor = RecordingProcessor()
    dispatcher = CommandDispatcher(
        store, processor, worker_id="commands", now=clock,
        query_capacity=20,
    )

    clock.advance(4)
    assert await dispatcher.tick_control() == 2
    assert processor.seen == [(11, "stop"), (12, "pause")]
    assert dispatcher.snapshot()["query_queue_depth"] == 1
    assert dispatcher.snapshot()["oldest_control_age_s"] == 4
    assert dispatcher.snapshot()["control_delay_warning"] is False

    clock.advance(2)
    store.persist_batch_and_offset(
        [{"update_id": 13, "operator_hash": operator_hash,
          "command": "stop", "received_at": NOW}],
        14,
    )
    await dispatcher.tick_control()
    assert dispatcher.snapshot()["control_delay_warning"] is True


@pytest.mark.anyio
async def test_deferred_control은_같은_tick에서_재claim하지않는다(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    operator_hash = "v1:" + "a" * 64
    store.persist_batch_and_offset(
        [{"update_id": 1, "operator_hash": operator_hash,
          "command": "stop", "received_at": clock()}],
        2,
    )

    class DeferredProcessor(RecordingProcessor):
        async def process_claimed(self, claimed):
            self.seen.append((claimed.update_id, claimed.command))
            return CommandResult("deferred")

    processor = DeferredProcessor()
    dispatcher = CommandDispatcher(
        store, processor, worker_id="commands", now=clock)
    assert await asyncio.wait_for(dispatcher.tick_control(), 0.1) == 0
    assert processor.seen == [(1, "stop")]


@pytest.mark.anyio
async def test_command응답은_outbox이고_confirmation원문은_DB에없다(tmp_path):
    clock = Clock()
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'response.db'}")
    Base.metadata.create_all(engine)
    notifications = NotificationStore(engine, now=clock)
    telegram = FakeTelegram()
    telegram.messages = []

    async def send_message(chat_id: int, text: str) -> int:
        telegram.messages.append(text)
        return len(telegram.messages)

    telegram.send_message = send_message
    circuit = TelegramCircuit()
    ephemeral = EphemeralResponseSender(
        telegram, chat_id=22, circuit=circuit, now=clock)
    outbox = LoopStep()
    sender = CompositeSender(ephemeral, outbox)
    publisher = CommandResponsePublisher(notifications, ephemeral)
    claimed = SimpleNamespace(
        update_id=9, command="account", correlation_id="telegram-9")

    await publisher.publish(
        claimed, CommandResult(
            "account", outbox_sensitive=True,
            response_text="예수금 1,000원"))
    assert notifications.count_outbox() == 1
    await publisher.publish(
        SimpleNamespace(
            update_id=10, command="resume", correlation_id="telegram-10"),
        CommandResult(
            "confirmation_required",
            confirmation_token="CONFIRM_SECRET",
            response_text="/confirm CONFIRM_SECRET",
            ephemeral=True))
    assert notifications.count_outbox() == 1
    assert "CONFIRM_SECRET" not in str(notifications.load_payload(1))
    assert await sender.run_once() == 1
    assert telegram.messages == ["/confirm CONFIRM_SECRET"]


@pytest.mark.anyio
async def test_명령응답_경로는_token_confirmation_계좌금액을_로그하지않는다(
        tmp_path, caplog):
    """Task 10 수용 회귀: mock sender를 써도 실제 명령 응답 흐름을 지난다."""
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'secret-log.db'}")
    Base.metadata.create_all(engine)
    notifications = NotificationStore(engine, now=clock)
    inbox = TelegramInboxStore(engine, now=clock)
    commands = TelegramCommandStore(engine, now=clock)
    telegram = FakeTelegram()
    telegram.messages = []

    async def send_message(chat_id: int, text: str) -> int:
        telegram.messages.append((chat_id, text))
        return len(telegram.messages)

    telegram.send_message = send_message
    bot_token = "BOT_TOKEN_SHOULD_NEVER_LOG"
    circuit = TelegramCircuit()
    ephemeral = EphemeralResponseSender(
        telegram, chat_id=22, circuit=circuit, now=clock)
    publisher = CommandResponsePublisher(notifications, ephemeral)

    class SensitiveControl:
        def scheduler_fingerprint(self):
            return "scheduler-state"

        async def account_summary(self):
            return {
                "available_deposit": "ACCOUNT_AMOUNT_SHOULD_NEVER_LOG",
                "total_eval": "ACCOUNT_AMOUNT_SHOULD_NEVER_LOG",
                "total_profit": "ACCOUNT_AMOUNT_SHOULD_NEVER_LOG",
                "realized_pnl": "ACCOUNT_AMOUNT_SHOULD_NEVER_LOG",
                "realized_pnl_confidence": "estimated",
                "as_of": "2026-07-24T10:00:00+09:00",
                "source": "fake",
                "failed_fields": (),
            }

        async def system_status(self):
            return {}

        async def open_positions_summary(self):
            return {}

        async def pause_scheduler(self):
            return {"applied": True}

        async def resume_scheduler(self, expected=None):
            assert expected == "scheduler-state"
            return {"applied": True}

        async def stop_new_entries(self, intent_id):
            return True

        async def liquidation_preview(self):
            return SimpleNamespace(targets=())

        async def liquidate_managed(self, intent_id, targets, *, expected_run_id=None):
            raise AssertionError("not used by account/resume regression")

        async def reconcile_control_intent(self, intent_id, targets=()):
            raise AssertionError("not used by account/resume regression")

    telegram.updates = [_message(89, "/account"), _message(90, "/resume")]
    poller = InboxPoller(
        telegram=telegram,
        inbox=inbox,
        operators=(OperatorIdentity(11, 22),),
        bot_token=SecretStr(bot_token),
        worker_id="secret-log-poller",
        circuit=circuit,
        now=clock,
    )
    assert await poller.run_once() == 2
    processor = CommandProcessor(
        inbox, commands, SensitiveControl(), "secret-log-command",
        chat_hash="v1:" + "c" * 64, now=clock)
    dispatcher = CommandDispatcher(
        inbox, processor, worker_id="secret-log-dispatcher", now=clock,
        response_publisher=publisher)
    assert await dispatcher.tick_control() == 1
    assert await dispatcher.tick_query() == 1
    assert await CompositeSender(ephemeral, LoopStep()).run_once() == 1

    confirmation = telegram.messages[0][1].removeprefix("/confirm ")
    assert confirmation and confirmation != telegram.messages[0][1]

    async def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example"})

    async with TelegramClient(
            SecretStr(bot_token), transport=httpx.MockTransport(redirect_handler)) as client:
        with pytest.raises(TelegramPermanentError):
            await client.get_updates(0)

    for sensitive_value in (
        bot_token,
        confirmation,
        "ACCOUNT_AMOUNT_SHOULD_NEVER_LOG",
    ):
        assert sensitive_value not in caplog.text


@pytest.mark.anyio
async def test_confirmation응답은_120초뒤_만료되어_전송하지않는다():
    clock = Clock()
    telegram = FakeTelegram()
    telegram.messages = []
    circuit = TelegramCircuit()

    async def send_message(chat_id: int, text: str) -> int:
        telegram.messages.append(text)
        return 1

    telegram.send_message = send_message
    sender = EphemeralResponseSender(
        telegram, chat_id=22, circuit=circuit, now=clock)
    await sender.enqueue("/confirm EXPIRED_SECRET")
    clock.advance(120)
    circuit.mark_dead("authentication_failed")

    assert await sender.run_once() == 0
    assert telegram.messages == []
    assert sender.snapshot()["pending"] == 0


@pytest.mark.anyio
async def test_query20건_초과는_원자거부하고_뒤_stop은_수신한다(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    operator_hash = "v1:" + "a" * 64
    store.persist_batch_and_offset(
        [{"update_id": index, "operator_hash": operator_hash,
          "command": "account", "received_at": clock()}
         for index in range(1, 21)],
        21,
    )
    telegram = FakeTelegram([
        _message(21, "/account"),
        _message(22, "/stop"),
    ])
    poller = InboxPoller(
        telegram=telegram, inbox=store,
        operators=(OperatorIdentity(11, 22),),
        bot_token=SecretStr("BOT-TOKEN"), worker_id="poller",
        circuit=TelegramCircuit(), now=clock)

    assert await poller.run_once() == 2
    assert store.pending_count({"account"}) == 20
    assert store.pending_count({"stop"}) == 1
    assert store.current_offset() == 23


def test_stale_poller는_새lease뒤_batch를_commit하지못한다(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    first = store.acquire_poller_lease("poller-a", 40)
    assert first is not None
    clock.advance(41)
    second = store.acquire_poller_lease("poller-b", 40)
    assert second is not None

    committed = store.persist_leased_poll_batch(
        "poller-a", first,
        [{"update_id": 1, "operator_hash": "v1:" + "a" * 64,
          "command": "stop", "received_at": clock()}],
        (), next_offset=2, minute=clock().replace(second=0, microsecond=0))
    assert committed is False
    assert store.current_offset() == 0
    assert store.count_updates() == 0


class LoopStep:
    def __init__(self, *, failures: int = 0) -> None:
        self.calls = 0
        self.failures = failures
        self.released = 0

    async def run_once(self) -> int:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("sensitive-value-must-not-be-used")
        return 0

    async def begin_shutdown(self) -> None:
        return None

    async def finish_shutdown(self) -> None:
        return None

    async def release_leases(self) -> None:
        self.released += 1

    def snapshot(self):
        return {"state": "running"}


@pytest.mark.anyio
async def test_독립loop_failure_budget과_telegram전용종료():
    poller = LoopStep(failures=99)
    dispatcher = LoopStep()
    projector = LoopStep()
    sender = LoopStep()
    maintenance = LoopStep()
    service = TelegramService(
        poller=poller,
        dispatcher=dispatcher,
        projector=projector,
        sender=sender,
        maintenance=maintenance,
        failure_budget=3,
        idle_s=0.001,
    )

    service.start()
    await asyncio.sleep(0.03)
    snapshot = service.snapshot()
    assert snapshot["poller"]["dead"] is True
    assert snapshot["sender"]["dead"] is False
    assert sender.calls > 0

    await service.begin_shutdown()
    await service.finish_shutdown(deadline_s=0.1)
    assert service.stop_trace == [
        "poller", "inbox_commit", "command_claims", "projector", "sender"
    ]
    assert sender.released == 1


@pytest.mark.anyio
async def test_429는_failure_budget을_즉시소진하지않고_retry_after를_기다린다():
    class RateLimitedStep(LoopStep):
        async def run_once(self):
            self.calls += 1
            from app.adapters.telegram import TelegramRateLimited
            raise TelegramRateLimited("getUpdates", 10)

    poller = RateLimitedStep()
    service = TelegramService(
        poller=poller, dispatcher=LoopStep(), projector=LoopStep(),
        sender=LoopStep(), maintenance=LoopStep(),
        failure_budget=3, idle_s=0.001)
    service.start()
    await asyncio.sleep(0.03)
    assert poller.calls == 1
    assert service.snapshot()["poller"]["dead"] is False
    await service.begin_shutdown()
    await service.finish_shutdown(deadline_s=0.1)


@pytest.mark.anyio
async def test_unknown대사는_느려도_control_lane을_막지않는다():
    entered, release = asyncio.Event(), asyncio.Event()

    class Dispatcher:
        control_calls = 0

        async def tick_control(self):
            self.control_calls += 1
            return 0

        async def tick_query(self):
            return 0

        async def reconcile_unknown(self):
            entered.set()
            await release.wait()
            return 0

        async def begin_shutdown(self):
            return None

        async def finish_shutdown(self):
            return None

        def snapshot(self):
            return {}

    dispatcher = Dispatcher()
    service = TelegramService(
        poller=LoopStep(), dispatcher=dispatcher, projector=LoopStep(),
        sender=LoopStep(), maintenance=LoopStep(), idle_s=0.001)
    service.start()
    await entered.wait()
    await asyncio.sleep(0.01)
    assert dispatcher.control_calls > 0
    release.set()
    await service.begin_shutdown()
    await service.finish_shutdown(deadline_s=0.1)


@pytest.mark.anyio
async def test_finish_shutdown은_lease반환stall에도_deadline을_넘기지않는다():
    release = asyncio.Event()

    class BlockingRelease(LoopStep):
        async def release_leases(self):
            await release.wait()

    service = TelegramService(
        poller=LoopStep(), dispatcher=LoopStep(), projector=LoopStep(),
        sender=BlockingRelease(), maintenance=LoopStep(),
        idle_s=0.001)
    service.start()
    await service.begin_shutdown()
    await asyncio.wait_for(service.finish_shutdown(deadline_s=0.01), 0.05)


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "kiwoom_app_key": "AK",
        "kiwoom_secret_key": "SK",
        "kiwoom_mock": True,
        "database_url": f"sqlite+pysqlite:///{tmp_path / 'lifespan.db'}",
        "scheduler_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_설정없음은_telegram비활성(tmp_path):
    from app.main import create_app

    app = create_app(_settings(tmp_path))
    with TestClient(app):
        assert app.state.telegram_service is None


def test_정상설정은_외부호출없이_telegram을_조립하고_닫는다(tmp_path, monkeypatch):
    from app import main

    settings = _settings(
        tmp_path,
        telegram_bot_token="BOT-TOKEN",
        telegram_allowed_user_id=11,
        telegram_allowed_chat_id=22,
    )
    engine = create_engine(settings.database_url.get_secret_value())
    Base.metadata.create_all(engine)
    engine.dispose()
    clients = []

    class NoNetworkTelegram(FakeTelegram):
        def __init__(self, token):
            super().__init__()
            self.closed = False
            self.messages = []
            clients.append(self)

        async def aclose(self):
            self.closed = True

        async def send_message(self, chat_id: int, text: str) -> int:
            self.messages.append((chat_id, text))
            return len(self.messages)

    monkeypatch.setattr(main, "TelegramClient", NoNetworkTelegram)
    app = main.create_app(settings)
    with TestClient(app):
        assert isinstance(app.state.telegram_service, TelegramService)
        assert app.state.telegram_client is clients[0]
        assert app.state.telegram_service.snapshot()["enabled"] is True
    assert clients[0].closed is True


@pytest.mark.anyio
async def test_main이_전체종료순서의_유일한소유자():
    from app.main import _shutdown_runtime

    trace = []

    class Telegram:
        async def begin_shutdown(self):
            trace.append("telegram_begin")

        async def finish_shutdown(self, deadline_s=10):
            trace.append("telegram_finish")

    class Scheduler:
        async def shutdown(self):
            trace.append("scheduler")

    class Trading:
        def __init__(self):
            self.task = asyncio.create_task(self._run())

        async def _run(self):
            try:
                await asyncio.Event().wait()
            finally:
                trace.append("trading")

        def current_task(self):
            return self.task

    class Idle:
        def current_task(self):
            return None

    state = SimpleNamespace(
        telegram_service=Telegram(),
        scheduler=Scheduler(),
        trading=Trading(),
        scoring=Idle(),
        collection=Idle(),
        analysis=Idle(),
    )
    await asyncio.sleep(0)
    await _shutdown_runtime(SimpleNamespace(state=state))
    assert trace == [
        "telegram_begin", "scheduler", "trading", "telegram_finish"
    ]
