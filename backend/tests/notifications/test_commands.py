import asyncio
import hashlib
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from app.domain.notifications.commands import CommandProcessor
from app.domain.trading.models import LiquidationResult
from app.store.models import Base
from app.store.telegram_command_store import TelegramCommandStore
from app.store.telegram_inbox_store import TelegramInboxStore


NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)
OP = "v1:" + hashlib.sha256(b"operator").hexdigest()
CHAT = "v1:" + "0" * 64


class Control:
    def __init__(self, calls, commands):
        self.calls = calls
        self.commands = commands
        self.liquidate_calls = []
        self.reconcile_calls = []
        self.calls_by_kind = []
        self._paused = True
        self.stop_applied = True
        self.delay_pause = False
        self.clock = None
        self.liquidation_status = "needs_attention"
        self.reconcile_outcomes = []

    async def stop_new_entries(self, intent_id):
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", intent_id)
        assert self.commands.intent_status(intent_id) == "running"
        self.calls.append("intent:10")
        self.calls.append(f"control:stop:{intent_id}")
        return self.stop_applied

    def scheduler_fingerprint(self):
        return "scheduler-state"

    async def system_status(self):
        self.calls_by_kind.append("status")
        return {"scheduler": {"paused": self._paused},
                "trading": {"kill_switch": "stop_new_entries"
                            if self.stop_applied else None}}

    async def account_summary(self):
        self.calls_by_kind.append("account")
        return {}

    async def open_positions_summary(self):
        self.calls_by_kind.append("positions")
        return {}

    async def pause_scheduler(self):
        self.calls_by_kind.append("pause")
        self._paused = True
        if self.delay_pause:
            await asyncio.sleep(0.02)
            self.clock.value += timedelta(seconds=2)
            await asyncio.sleep(0.03)
        return {}

    async def resume_scheduler(self, expected=None):
        assert expected == "scheduler-state"
        self.calls_by_kind.append("resume")
        self._paused = False
        return {}

    async def liquidation_preview(self):
        return SimpleNamespace(targets=(SimpleNamespace(
            position_id=7, symbol="005930", quantity=3),))

    async def liquidate_managed(self, intent_id, targets, *, expected_run_id=None):
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", intent_id)
        self.liquidate_calls.append((intent_id, tuple(targets), expected_run_id))
        return LiquidationResult(self.liquidation_status, False, "manual check")

    async def reconcile_control_intent(self, intent_id, targets=()):
        self.reconcile_calls.append((intent_id, tuple(targets)))
        if self.reconcile_outcomes:
            return LiquidationResult(self.reconcile_outcomes.pop(0), False, None)
        return LiquidationResult("needs_attention", False, "open sell remains")


@pytest.fixture
def processor(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'commands.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    calls = []
    return (CommandProcessor(inbox, commands, Control(calls, commands), "worker",
                             chat_hash=CHAT, now=lambda: NOW), inbox, commands, calls)


@pytest.mark.anyio
async def test_stop은_intent가_먼저_영속된_뒤_control을_호출한다(processor):
    processor, inbox, commands, calls = processor
    inbox.persist_batch_and_offset([
        {"update_id": 10, "operator_hash": OP, "command": "stop", "received_at": NOW}
    ], 11)

    await processor.process_next()

    assert calls == ["intent:10", "control:stop:telegram_command_update_10"]
    assert commands.intent_status("telegram_command_update_10") == "succeeded"
    assert commands.audit_results() == ["succeeded"]


@pytest.mark.anyio
async def test_stop이_control에_적용되지않으면_성공으로_감사하지않는다(processor):
    processor, inbox, commands, _ = processor
    processor._control.stop_applied = False
    inbox.persist_batch_and_offset([
        {"update_id": 9, "operator_hash": OP, "command": "stop", "received_at": NOW}
    ], 10)

    result = await processor.process_next()

    assert result.kind == "needs_attention"
    assert commands.intent_status("telegram_command_update_9") == "needs_attention"


@pytest.mark.anyio
async def test_긴control호출동안_heartbeat가_running_lease를_갱신한다(tmp_path):
    class Clock:
        value = NOW

        def __call__(self):
            return self.value

    clock = Clock()
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'heartbeat.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=clock)
    commands = TelegramCommandStore(engine, now=clock)
    control = Control([], commands)
    control.delay_pause, control.clock = True, clock
    worker = CommandProcessor(inbox, commands, control, "worker",
                              chat_hash=CHAT, now=clock,
                              execution_lease_s=1, heartbeat_s=0.005)
    inbox.persist_batch_and_offset([
        {"update_id": 8, "operator_hash": OP, "command": "pause", "received_at": NOW}
    ], 9)

    running = asyncio.create_task(worker.process_next())
    await asyncio.sleep(0.035)

    assert await worker.reconcile_unknown() is None
    assert (await running).kind == "pause"
    assert commands.intent_status("telegram_command_update_8") == "succeeded"


@pytest.mark.anyio
async def test_liquidate첫요청은_청산하지_않고_confirmation만_발급한다(processor):
    processor, inbox, _, _ = processor
    inbox.persist_batch_and_offset([
        {"update_id": 11, "operator_hash": OP, "command": "liquidate_all", "received_at": NOW}
    ], 12)

    result = await processor.process_next()

    assert result.kind == "confirmation_required"
    assert result.confirmation_token is not None
    assert processor._control.liquidate_calls == []


@pytest.mark.anyio
async def test_confirmed_liquidation은_durable_intent를_통해서만_control을_호출한다(processor):
    processor, inbox, _, _ = processor
    inbox.persist_batch_and_offset([
        {"update_id": 11, "operator_hash": OP, "command": "liquidate_all", "received_at": NOW}
    ], 12)
    first = await processor.process_next()
    digest = hashlib.sha256(first.confirmation_token.encode()).hexdigest()
    inbox.persist_batch_and_offset([
        {"update_id": 12, "operator_hash": OP, "command": "confirm",
         "argument_hash": digest, "received_at": NOW}
    ], 13)

    result = await processor.process_next()

    assert result.kind == "needs_attention"
    [(intent_id, targets, expected_run_id)] = processor._control.liquidate_calls
    assert intent_id == "telegram_command_confirmation_1"
    assert targets[0].position_id == 7 and targets[0].quantity == 3
    assert expected_run_id is None


@pytest.mark.anyio
async def test_confirm응답영속실패_재처리는_소비토큰오류나_재청산을_만들지않는다(
        processor):
    processor, inbox, _, _ = processor
    inbox.persist_batch_and_offset([
        {"update_id": 11, "operator_hash": OP, "command": "liquidate_all",
         "received_at": NOW}
    ], 12)
    issued = await processor.process_next()
    inbox.persist_batch_and_offset([
        {"update_id": 12, "operator_hash": OP, "command": "confirm",
         "argument_hash": hashlib.sha256(
             issued.confirmation_token.encode()).hexdigest(),
         "received_at": NOW}
    ], 13)
    first_claim = inbox.claim_next("dispatcher")
    first = await processor.process_claimed(first_claim)
    assert first.kind == "needs_attention"
    assert inbox.release(12, "dispatcher", first_claim.version)

    retry_claim = inbox.claim_next("dispatcher")
    retry = await processor.process_claimed(retry_claim)

    assert retry.kind == "needs_attention"
    assert "기존 처리 결과" in retry.response_text
    assert len(processor._control.liquidate_calls) == 1


@pytest.mark.anyio
async def test_confirm소비직후_crash의_pending_intent를_재처리가_회수한다(
        processor):
    processor, inbox, commands, _ = processor
    inbox.persist_batch_and_offset([
        {"update_id": 21, "operator_hash": OP, "command": "liquidate_all",
         "received_at": NOW}
    ], 22)
    issued = await processor.process_next()
    digest = hashlib.sha256(issued.confirmation_token.encode()).hexdigest()
    inbox.persist_batch_and_offset([
        {"update_id": 22, "operator_hash": OP, "command": "confirm",
         "argument_hash": digest, "received_at": NOW}
    ], 23)
    fingerprint, targets = await processor._liquidation_context()
    intent = commands.consume_and_create_intent(
        digest, OP, CHAT, "liquidate_all", fingerprint, NOW,
        update_id=22, targets=targets)
    assert commands.intent_status(intent.id) == "pending"

    claimed = inbox.claim_next("dispatcher")
    result = await processor.process_claimed(claimed)

    assert result.kind == "needs_attention"
    assert commands.intent_status(intent.id) == "needs_attention"
    assert len(processor._control.liquidate_calls) == 1


@pytest.mark.anyio
async def test_accepted_liquidation은_terminal대사까지_lease를_유지한다(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'accepted.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    control = Control([], commands)
    control.liquidation_status = "accepted"
    control.reconcile_outcomes = ["in_progress", "in_progress", "needs_attention"]
    worker = CommandProcessor(inbox, commands, control, "worker",
                                  chat_hash=CHAT, now=lambda: NOW,
                              execution_lease_s=1, heartbeat_s=0.005)
    inbox.persist_batch_and_offset([
        {"update_id": 41, "operator_hash": OP, "command": "liquidate_all", "received_at": NOW}
    ], 42)
    confirmation = await worker.process_next()
    inbox.persist_batch_and_offset([
        {"update_id": 42, "operator_hash": OP, "command": "confirm",
         "argument_hash": hashlib.sha256(confirmation.confirmation_token.encode()).hexdigest(),
         "received_at": NOW}
    ], 43)

    result = await worker.process_next()
    await asyncio.sleep(0.008)

    assert result.kind == "liquidation_accepted"
    assert commands.intent_status("telegram_command_confirmation_1") == "running"
    assert await worker.reconcile_unknown() is None
    for _ in range(20):
        if commands.intent_status("telegram_command_confirmation_1") != "running":
            break
        await asyncio.sleep(0.005)
    assert commands.intent_status("telegram_command_confirmation_1") == "needs_attention"


@pytest.mark.anyio
async def test_running_liquidation재기동은_control대사만하고_재청산하지않는다(processor):
    processor, inbox, commands, _ = processor
    issued = commands.issue_confirmation(OP, CHAT, "liquidate_all", "fingerprint")
    digest = hashlib.sha256(issued.raw_token.encode()).hexdigest()
    inbox.persist_batch_and_offset([
        {"update_id": 70, "operator_hash": OP, "command": "confirm",
         "argument_hash": digest, "received_at": NOW}
    ], 71)
    intent = commands.consume_and_create_intent(
        digest, OP, CHAT, "liquidate_all", "fingerprint", NOW,
        update_id=70, targets=[{"position_id": 7, "symbol": "005930", "quantity": 3}])
    claimed = commands.claim_intent_by_id(intent.id, "old-worker")
    assert commands.mark_running(intent.id, "old-worker", claimed.version)
    assert commands.mark_unknown(intent.id, "old-worker", claimed.version + 1)

    result = await processor.reconcile_unknown()

    assert result.kind == "needs_attention"
    assert processor._control.reconcile_calls[0][0] == intent.id
    assert processor._control.reconcile_calls[0][1][0].position_id == 7
    assert processor._control.liquidate_calls == []
    assert commands.intent_status(intent.id) == "needs_attention"


@pytest.mark.anyio
async def test_running_pause재기동은_현재paused상태로만_성공을판정한다(processor):
    processor, inbox, commands, _ = processor
    inbox.persist_batch_and_offset([
        {"update_id": 71, "operator_hash": OP, "command": "pause", "received_at": NOW}
    ], 72)
    intent = commands.create_intent_for_update(71, "pause")
    claimed = commands.claim_intent_by_id(intent.id, "old-worker")
    assert commands.mark_running(intent.id, "old-worker", claimed.version)
    assert commands.mark_unknown(intent.id, "old-worker", claimed.version + 1)
    processor._control._paused = True

    result = await processor.reconcile_unknown()

    assert result.kind == "succeeded"
    assert commands.intent_status(intent.id) == "succeeded"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("command", "expected"),
    [("status", "status"), ("account", "account"), ("positions", "positions"),
     ("pause", "pause"), ("stop", "stop"), ("resume", "confirmation_required"),
     ("liquidate_all", "confirmation_required"), ("help", "help")])
async def test_지원명령은_명시된_결과로_수렴(processor, command, expected):
    processor, inbox, _, _ = processor
    inbox.persist_batch_and_offset([
        {"update_id": 100, "operator_hash": OP, "command": command, "received_at": NOW}
    ], 101)

    result = await processor.process_next()

    assert result.kind == expected
    if command in {"account", "positions"}:
        assert result.outbox_sensitive is True
