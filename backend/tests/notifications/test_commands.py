import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

import app.domain.notifications.commands as command_module
from app.core.operations_control import OperationsControl
from app.domain.broker import Balance, Position
from app.domain.notifications.commands import CommandProcessor
from app.domain.notifications.analysis_summary import (
    AnalysisVerdictSummary,
    MorningAnalysisSummary,
    render_analysis_summary,
)
from app.domain.notifications.digest import render_retained_digest
from app.domain.notifications.models import CommandKind
from app.domain.trading.models import (
    LiquidationReason,
    LiquidationResult,
    PositionState,
    TradePosition,
)
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
        self.liquidation_warning = "manual check"
        self.liquidation_reason = None
        self.account_fully_empty = False
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
        return LiquidationResult(
            self.liquidation_status,
            self.account_fully_empty,
            self.liquidation_warning,
            reason=self.liquidation_reason,
        )

    async def reconcile_control_intent(self, intent_id, targets=()):
        self.reconcile_calls.append((intent_id, tuple(targets)))
        if self.reconcile_outcomes:
            outcome = self.reconcile_outcomes.pop(0)
            if isinstance(outcome, tuple):
                status, reason = outcome
            else:
                status, reason = outcome, None
            return LiquidationResult(status, False, None, reason=reason)
        return LiquidationResult("needs_attention", False, "open sell remains")


class AnalysisReports:
    def __init__(self, summary=None):
        self.summary = summary
        self.calls = 0

    def latest_analysis(self):
        self.calls += 1
        return self.summary


class DigestReports:
    def __init__(self, payload=None):
        self.payload = payload
        self.calls = 0

    def latest_digest(self):
        self.calls += 1
        return ((self.payload, (render_retained_digest(self.payload),))
                if self.payload is not None else None)


def _digest_payload():
    return {
        "version": 1, "trading_day": "2026-07-24", "run_environment": "mock",
        "pipeline": {"facts": {"collection_status": "done"}, "as_of": None,
                     "failed_fields": []},
        "trading": {"facts": {"order_count": 1}, "as_of": None,
                    "failed_fields": []},
        "account": {
            "available_deposit": None, "total_eval": None, "total_profit": None,
            "realized_pnl": None, "realized_pnl_confidence": "unknown",
            "source": "unavailable", "failed_fields": ["account_snapshot"],
            "as_of": None, "trading_day": None,
        },
    }


def _analysis_summary() -> MorningAnalysisSummary:
    return MorningAnalysisSummary(
        run_id=3,
        run_environment="mock",
        regime="neutral",
        market_summary="방향성 확인 중",
        max_picks_advice=1,
        score_reference_date=NOW.date(),
        started_at=NOW,
        finished_at=NOW,
        verdicts=(AnalysisVerdictSummary(
            symbol="005930", name="삼성전자", verdict="approve",
            confidence=0.7, reasons=("근거",), risk_flags=("위험",),
            picked=True, pick_rank=1,
        ),),
    )


class OperationsScheduler:
    def snapshot(self):
        return {"paused": False, "dead": False, "enabled": True}


class OperationsTrading:
    def __init__(self, request_outcome, reconcile_outcome):
        self.request_outcome = request_outcome
        self.reconcile_outcome = reconcile_outcome
        self.request_calls = 0
        self.reconcile_calls = 0

    def progress(self):
        return {
            "run_id": None,
            "status": "idle",
            "positions_count": 1,
            "kill_switch": None,
        }

    async def request_managed_liquidation(
        self,
        _intent_id,
        _targets,
        *,
        expected_run_id=None,
    ):
        assert expected_run_id is None
        self.request_calls += 1
        return self.request_outcome

    async def reconcile_control_intent(self, _intent_id, _targets=()):
        self.reconcile_calls += 1
        return self.reconcile_outcome


class OperationsStore:
    def open_positions(self, _environment):
        position = TradePosition(
            "005930",
            "삼성전자",
            "kospi",
            PositionState.ENTERED,
            100,
            3,
            100,
            False,
            entered_at=NOW,
        )
        return [(7, position)], []


class OperationsBroker:
    async def get_balance(self):
        return Balance(
            (
                Position("005930", "삼성전자", 3, 100, 110, 330),
                Position("000660", "SK하이닉스", 2, 200, 210, 420),
            ),
            750,
            50,
        )


def operations_control(request_outcome, reconcile_outcome):
    trading = OperationsTrading(request_outcome, reconcile_outcome)
    return (
        OperationsControl(
            OperationsScheduler(),
            trading,
            OperationsStore(),
            OperationsBroker(),
            SimpleNamespace(KST=timezone.utc),
            "mock",
            now=lambda: NOW,
        ),
        trading,
    )


class PresenterSpy:
    def __init__(self):
        self.calls = []

    def status(self, value):
        self.calls.append(("status", value))
        return "presented:status"

    def account(self, value):
        self.calls.append(("account", value))
        return "presented:account"

    def positions(self, value):
        self.calls.append(("positions", value))
        return "presented:positions"

    def help(self):
        self.calls.append(("help",))
        return "presented:help"

    def confirmation(self, kind, command):
        self.calls.append(("confirmation", kind, command))
        return f"presented:confirmation:{kind.value}"

    def control_result(self, kind, status, *, applied=True):
        self.calls.append(("control_result", kind, status, applied))
        return f"presented:control:{kind.value}:{status}:{applied}"

    def existing_result(self, kind, status):
        self.calls.append(("existing_result", kind, status))
        return f"presented:existing:{kind.value}:{status}"


@pytest.fixture
def processor(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'commands.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    calls = []
    return (CommandProcessor(inbox, commands, Control(calls, commands), "worker",
                             chat_hash=CHAT, now=lambda: NOW,
                             analysis_reports=AnalysisReports(),
                             digest_reports=DigestReports()), inbox, commands, calls)


@pytest.mark.anyio
async def test_analysis는_저장된_요약만_조회하고_control을_호출하지않는다(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'analysis.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    control = Control([], commands)
    summary = _analysis_summary()
    reports = AnalysisReports(summary)
    worker = CommandProcessor(
        inbox, commands, control, "worker", chat_hash=CHAT, now=lambda: NOW,
        analysis_reports=reports, digest_reports=DigestReports(),
    )
    inbox.persist_batch_and_offset(
        [{"update_id": 30, "operator_hash": OP, "command": "analysis", "received_at": NOW}],
        31,
    )

    result = await worker.process_next()

    assert result.kind == "analysis"
    assert result.outbox_sensitive is True
    assert result.response_text == render_analysis_summary(summary)
    assert control.calls == []
    assert control.calls_by_kind == []
    assert reports.calls == 1


@pytest.mark.anyio
async def test_analysis는_저장된성공분석이없으면_명시적으로알린다(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'analysis-none.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    control = Control([], commands)
    reports = AnalysisReports()
    worker = CommandProcessor(
        inbox, commands, control, "worker", chat_hash=CHAT, now=lambda: NOW,
        analysis_reports=reports, digest_reports=DigestReports(),
    )
    inbox.persist_batch_and_offset(
        [{"update_id": 31, "operator_hash": OP, "command": "analysis", "received_at": NOW}],
        32,
    )

    result = await worker.process_next()

    assert result.kind == "analysis"
    assert result.outbox_sensitive is True
    assert result.response_text == "🧠 최근 AI 분석\n\n조회 가능한 AI 분석이 없습니다."
    assert control.calls == []
    assert control.calls_by_kind == []
    assert reports.calls == 1


@pytest.mark.anyio
async def test_analysis는_표시실패를_fallback으로격리하고_intent를성공처리한다(
    tmp_path, monkeypatch, caplog,
):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'analysis-fallback.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    control = Control([], commands)
    reports = AnalysisReports(_analysis_summary())
    worker = CommandProcessor(
        inbox, commands, control, "worker", chat_hash=CHAT,
        now=lambda: NOW, analysis_reports=reports,
        digest_reports=DigestReports(),
    )
    inbox.persist_batch_and_offset(
        [{"update_id": 36, "operator_hash": OP, "command": "analysis",
          "received_at": NOW}],
        37,
    )

    def fail_presentation(_summary):
        raise RuntimeError("synthetic presenter failure")

    monkeypatch.setattr(
        command_module, "render_analysis_summary", fail_presentation)

    result = await worker.process_next()

    assert result.kind == "analysis"
    assert result.outbox_sensitive is True
    assert result.response_text == "🧠 최근 AI 분석\n\n조회 가능한 AI 분석이 없습니다."
    assert commands.intent_status("telegram_command_update_36") == "succeeded"
    assert control.calls == []
    assert control.calls_by_kind == []
    assert reports.calls == 1
    presentation_logs = [
        record.getMessage()
        for record in caplog.records
        if "telegram command presentation failed" in record.getMessage()
    ]
    assert presentation_logs == [
        (
            "telegram command presentation failed "
            "method=fail_presentation exception_type=RuntimeError"
        )
    ]
    assert "synthetic presenter failure" not in presentation_logs[0]


@pytest.mark.anyio
async def test_digest는_보존payload만_렌더링하고_control을_호출하지않는다(tmp_path):
    """현재 계좌/broker를 호출해 과거 digest를 재계산하면 실패한다."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'digest.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    control = Control([], commands)
    reports = DigestReports(_digest_payload())
    worker = CommandProcessor(
        inbox, commands, control, "worker", chat_hash=CHAT, now=lambda: NOW,
        analysis_reports=AnalysisReports(), digest_reports=reports,
    )
    inbox.persist_batch_and_offset(
        [{"update_id": 34, "operator_hash": OP, "command": "digest", "received_at": NOW}],
        35,
    )

    result = await worker.process_next()

    assert result.kind == "digest"
    assert result.outbox_sensitive is True
    assert result.response_text == render_retained_digest(_digest_payload())
    assert control.calls == []
    assert control.calls_by_kind == []
    assert reports.calls == 1


@pytest.mark.anyio
async def test_digest는_보존결과가없으면_명시적으로알린다(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'digest-none.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    control = Control([], commands)
    reports = DigestReports()
    worker = CommandProcessor(
        inbox, commands, control, "worker", chat_hash=CHAT, now=lambda: NOW,
        analysis_reports=AnalysisReports(), digest_reports=reports,
    )
    inbox.persist_batch_and_offset(
        [{"update_id": 35, "operator_hash": OP, "command": "digest", "received_at": NOW}],
        36,
    )

    result = await worker.process_next()

    assert result.kind == "digest"
    assert result.outbox_sensitive is True
    assert result.response_text == (
        "📋 최근 거래 다이제스트\n\n조회 가능한 최근 거래 다이제스트가 없습니다.")
    assert control.calls == []
    assert control.calls_by_kind == []
    assert reports.calls == 1


@pytest.mark.anyio
async def test_digest_terminal은_보존결과만_다시표시한다(tmp_path):
    """완료된 query intent가 현재 control 상태를 재조회하면 실패한다."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'digest-terminal.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    control = Control([], commands)
    reports = DigestReports(_digest_payload())
    inbox.persist_batch_and_offset(
        [{"update_id": 36, "operator_hash": OP, "command": "digest", "received_at": NOW}],
        37,
    )
    intent = commands.create_intent_for_update(36, "digest")
    claimed = commands.claim_intent_by_id(intent.id, "old-worker")
    assert claimed is not None
    assert commands.mark_running(intent.id, "old-worker", claimed.version)
    assert commands.mark_terminal(intent.id, "old-worker", claimed.version + 1, "succeeded")
    worker = CommandProcessor(
        inbox, commands, control, "worker", chat_hash=CHAT, now=lambda: NOW,
        analysis_reports=AnalysisReports(), digest_reports=reports,
    )

    result = await worker.process_next()

    assert result.response_text == render_retained_digest(_digest_payload())
    assert reports.calls == 1
    assert control.calls == []
    assert control.calls_by_kind == []


@pytest.mark.anyio
async def test_digest_unknown대사는_현재값을재조회하지않고_성공으로_재조정한다(tmp_path):
    """unknown digest를 재조정할 때 broker/control을 부르면 실패한다."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'digest-reconcile.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    control = Control([], commands)
    reports = DigestReports(_digest_payload())
    inbox.persist_batch_and_offset(
        [{"update_id": 37, "operator_hash": OP, "command": "digest", "received_at": NOW}],
        38,
    )
    intent = commands.create_intent_for_update(37, "digest")
    claimed = commands.claim_intent_by_id(intent.id, "old-worker")
    assert claimed is not None
    assert commands.mark_running(intent.id, "old-worker", claimed.version)
    assert commands.mark_owned_running_unknown("old-worker") == 1
    worker = CommandProcessor(
        inbox, commands, control, "worker", chat_hash=CHAT, now=lambda: NOW,
        analysis_reports=AnalysisReports(), digest_reports=reports,
    )

    result = await worker.reconcile_unknown()

    assert result is not None and result.kind == "succeeded"
    assert commands.intent_status(intent.id) == "succeeded"
    assert reports.calls == 0
    assert control.calls == []
    assert control.calls_by_kind == []


@pytest.mark.anyio
async def test_analysis_terminal은_저장된결과만_다시표시한다(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'analysis-terminal.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    control = Control([], commands)
    summary = _analysis_summary()
    reports = AnalysisReports(summary)
    inbox.persist_batch_and_offset(
        [{"update_id": 32, "operator_hash": OP, "command": "analysis", "received_at": NOW}],
        33,
    )
    intent = commands.create_intent_for_update(32, "analysis")
    claimed = commands.claim_intent_by_id(intent.id, "old-worker")
    assert claimed is not None
    assert commands.mark_running(intent.id, "old-worker", claimed.version)
    assert commands.mark_terminal(intent.id, "old-worker", claimed.version + 1, "succeeded")
    worker = CommandProcessor(
        inbox, commands, control, "worker", chat_hash=CHAT, now=lambda: NOW,
        analysis_reports=reports, digest_reports=DigestReports(),
    )

    result = await worker.process_next()

    assert result.response_text == render_analysis_summary(summary)
    assert reports.calls == 1
    assert control.calls == []
    assert control.calls_by_kind == []


@pytest.mark.anyio
async def test_analysis_unknown대사는_성공으로_재조정하고_분석을재조회하지않는다(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'analysis-reconcile.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    control = Control([], commands)
    reports = AnalysisReports(_analysis_summary())
    inbox.persist_batch_and_offset(
        [{"update_id": 33, "operator_hash": OP, "command": "analysis", "received_at": NOW}],
        34,
    )
    intent = commands.create_intent_for_update(33, "analysis")
    claimed = commands.claim_intent_by_id(intent.id, "old-worker")
    assert claimed is not None
    assert commands.mark_running(intent.id, "old-worker", claimed.version)
    assert commands.mark_owned_running_unknown("old-worker") == 1
    worker = CommandProcessor(
        inbox, commands, control, "worker", chat_hash=CHAT, now=lambda: NOW,
        analysis_reports=reports, digest_reports=DigestReports(),
    )

    result = await worker.reconcile_unknown()

    assert result is not None and result.kind == "succeeded"
    assert commands.intent_status(intent.id) == "succeeded"
    assert reports.calls == 0
    assert control.calls == []
    assert control.calls_by_kind == []


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
                              execution_lease_s=1, heartbeat_s=0.005,
                              analysis_reports=AnalysisReports(),
                              digest_reports=DigestReports())
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
async def test_구조화청산사유만응답에쓰고_warning원문은노출로그하지않는다(
    processor, caplog
):
    caplog.set_level(logging.DEBUG)
    processor, inbox, _, _ = processor
    control = processor._control
    order_marker = "broker-order-SECRET"
    quantity_marker = "DB수량=9 broker수량=8"
    sensitive_warning = f"005930 {order_marker} {quantity_marker}"
    control.liquidation_reason = LiquidationReason.QUANTITY_MISMATCH
    control.liquidation_warning = sensitive_warning
    inbox.persist_batch_and_offset(
        [
            {
                "update_id": 31,
                "operator_hash": OP,
                "command": "liquidate_all",
                "received_at": NOW,
            }
        ],
        32,
    )
    issued = await processor.process_next()
    inbox.persist_batch_and_offset(
        [
            {
                "update_id": 32,
                "operator_hash": OP,
                "command": "confirm",
                "argument_hash": hashlib.sha256(
                    issued.confirmation_token.encode()
                ).hexdigest(),
                "received_at": NOW,
            }
        ],
        33,
    )

    result = await processor.process_next()

    assert "관리 기록과 브로커 잔고 수량이 일치하지 않습니다" in result.response_text
    assert sensitive_warning not in result.response_text
    for marker in (sensitive_warning, order_marker, quantity_marker):
        assert marker not in caplog.text


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
    assert retry.response_text.startswith("🚨 처리 결과를 확인해야 합니다")
    assert len(processor._control.liquidate_calls) == 1


@pytest.mark.anyio
async def test_청산성공reason누락은_terminal과재처리에서도_needs_attention이다(
        processor):
    processor, inbox, commands, _ = processor
    processor._control.liquidation_status = "succeeded"
    processor._control.liquidation_reason = None
    inbox.persist_batch_and_offset([
        {"update_id": 13, "operator_hash": OP, "command": "liquidate_all",
         "received_at": NOW}
    ], 14)
    issued = await processor.process_next()
    inbox.persist_batch_and_offset([
        {"update_id": 14, "operator_hash": OP, "command": "confirm",
         "argument_hash": hashlib.sha256(
             issued.confirmation_token.encode()).hexdigest(),
         "received_at": NOW}
    ], 15)

    first_claim = inbox.claim_next("dispatcher")
    first = await processor.process_claimed(first_claim)

    assert first.kind == "needs_attention"
    assert commands.intent_status(
        "telegram_command_confirmation_1"
    ) == "needs_attention"
    assert inbox.release(14, "dispatcher", first_claim.version)

    retry_claim = inbox.claim_next("dispatcher")
    retry = await processor.process_claimed(retry_claim)

    assert retry.kind == "needs_attention"
    assert retry.response_text.startswith("🚨 처리 결과를 확인해야 합니다")
    assert len(processor._control.liquidate_calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "damaged_reason",
    [None, LiquidationReason.MARKET_CLOSED],
    ids=["missing-reason", "mismatched-reason"],
)
async def test_operations_control의_손상된성공은_미관리잔고가있어도_fail_closed한다(
    tmp_path,
    damaged_reason,
):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'operations-control.db'}"
    )
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)

    class Scheduler:
        def snapshot(self):
            return {"paused": False, "dead": False, "enabled": True}

    class Trading:
        calls = 0

        def progress(self):
            return {
                "run_id": None,
                "status": "idle",
                "positions_count": 1,
                "kill_switch": None,
            }

        async def request_managed_liquidation(
            self,
            _intent_id,
            _targets,
            *,
            expected_run_id=None,
        ):
            assert expected_run_id is None
            self.calls += 1
            return LiquidationResult(
                "succeeded",
                False,
                "damaged downstream contract",
                reason=damaged_reason,
            )

    class Store:
        def open_positions(self, _environment):
            position = TradePosition(
                "005930",
                "삼성전자",
                "kospi",
                PositionState.ENTERED,
                100,
                3,
                100,
                False,
                entered_at=NOW,
            )
            return [(7, position)], []

    class Broker:
        async def get_balance(self):
            return Balance(
                (
                    Position("005930", "삼성전자", 3, 100, 110, 330),
                    Position("000660", "SK하이닉스", 2, 200, 210, 420),
                ),
                750,
                50,
            )

    trading = Trading()
    control = OperationsControl(
        Scheduler(),
        trading,
        Store(),
        Broker(),
        SimpleNamespace(KST=timezone.utc),
        "mock",
        now=lambda: NOW,
    )
    processor = CommandProcessor(
        inbox,
        commands,
        control,
        "worker",
        chat_hash=CHAT,
        now=lambda: NOW,
        analysis_reports=AnalysisReports(), digest_reports=DigestReports(),
    )
    inbox.persist_batch_and_offset(
        [
            {
                "update_id": 17,
                "operator_hash": OP,
                "command": "liquidate_all",
                "received_at": NOW,
            }
        ],
        18,
    )
    issued = await processor.process_next()
    inbox.persist_batch_and_offset(
        [
            {
                "update_id": 18,
                "operator_hash": OP,
                "command": "confirm",
                "argument_hash": hashlib.sha256(
                    issued.confirmation_token.encode()
                ).hexdigest(),
                "received_at": NOW,
            }
        ],
        19,
    )

    first_claim = inbox.claim_next("dispatcher")
    first = await processor.process_claimed(first_claim)

    assert first.kind == "needs_attention"
    assert commands.intent_status(
        "telegram_command_confirmation_1"
    ) == "needs_attention"
    persisted, _ = commands.intent_for_update(18)
    assert persisted.terminal_reason is None
    assert inbox.release(18, "dispatcher", first_claim.version)

    retry_claim = inbox.claim_next("dispatcher")
    retry = await processor.process_claimed(retry_claim)

    assert retry.kind == "needs_attention"
    assert retry.response_text.startswith("🚨 처리 결과를 확인해야 합니다")
    assert trading.calls == 1


@pytest.mark.anyio
async def test_손상된terminal사유는_영속하거나_재물질화하지않는다(
    processor,
):
    processor, inbox, commands, _ = processor
    processor._control.liquidation_status = "needs_attention"
    processor._control.liquidation_reason = LiquidationReason.COMPLETED
    inbox.persist_batch_and_offset(
        [
            {
                "update_id": 19,
                "operator_hash": OP,
                "command": "liquidate_all",
                "received_at": NOW,
            }
        ],
        20,
    )
    issued = await processor.process_next()
    inbox.persist_batch_and_offset(
        [
            {
                "update_id": 20,
                "operator_hash": OP,
                "command": "confirm",
                "argument_hash": hashlib.sha256(
                    issued.confirmation_token.encode()
                ).hexdigest(),
                "received_at": NOW,
            }
        ],
        21,
    )
    first_claim = inbox.claim_next("dispatcher")

    first = await processor.process_claimed(first_claim)

    assert first.kind == "needs_attention"
    persisted, _ = commands.intent_for_update(20)
    assert persisted.terminal_reason is None
    assert inbox.release(20, "dispatcher", first_claim.version)
    retry_claim = inbox.claim_next("dispatcher")
    retry = await processor.process_claimed(retry_claim)
    assert retry.kind == "needs_attention"
    assert retry.response_text.startswith("🚨 처리 결과를 확인해야 합니다")


@pytest.mark.anyio
async def test_기존청산성공의_terminal사유누락은_재물질화에서_fail_closed한다(
    processor,
):
    processor, inbox, commands, _ = processor
    issued = commands.issue_confirmation(
        OP,
        CHAT,
        "liquidate_all",
        "fingerprint",
    )
    digest = hashlib.sha256(issued.raw_token.encode()).hexdigest()
    inbox.persist_batch_and_offset(
        [
            {
                "update_id": 22,
                "operator_hash": OP,
                "command": "confirm",
                "argument_hash": digest,
                "received_at": NOW,
            }
        ],
        23,
    )
    intent = commands.consume_and_create_intent(
        digest,
        OP,
        CHAT,
        "liquidate_all",
        "fingerprint",
        NOW,
        update_id=22,
        targets=[
            {"position_id": 7, "symbol": "005930", "quantity": 3}
        ],
    )
    claimed = commands.claim_intent_by_id(intent.id, "old-worker")
    assert commands.mark_running(
        intent.id,
        "old-worker",
        claimed.version,
    )
    assert commands.mark_terminal(
        intent.id,
        "old-worker",
        claimed.version + 1,
        "succeeded",
    )

    inbox_claim = inbox.claim_next("dispatcher")
    result = await processor.process_claimed(inbox_claim)

    assert result.kind == "needs_attention"
    assert result.response_text.startswith("🚨 처리 결과를 확인해야 합니다")
    assert processor._control.liquidate_calls == []
    assert processor._control.reconcile_calls == []


@pytest.mark.parametrize(
    (
        "reconcile_outcome",
        "expected_status",
        "expected_terminal_reason",
        "expected_prefix",
    ),
    [
        (
            LiquidationResult(
                "succeeded",
                False,
                None,
                reason=LiquidationReason.COMPLETED,
            ),
            "succeeded",
            "unmanaged_balance",
            "⚠️ 관리 포지션 청산 완료",
        ),
        (
            LiquidationResult(
                "needs_attention",
                False,
                None,
                reason=LiquidationReason.COMPLETED,
            ),
            "needs_attention",
            None,
            "🚨 처리 결과를 확인해야 합니다",
        ),
    ],
    ids=["valid-unmanaged", "damaged-terminal-reason"],
)
@pytest.mark.anyio
async def test_operations_control_monitor완료는_미관리잔고결과를_재물질화한다(
    tmp_path,
    reconcile_outcome,
    expected_status,
    expected_terminal_reason,
    expected_prefix,
):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'monitor-unmanaged.db'}"
    )
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    control, trading = operations_control(
        LiquidationResult(
            "accepted",
            False,
            None,
            reason=LiquidationReason.ACCEPTED,
        ),
        reconcile_outcome,
    )
    processor = CommandProcessor(
        inbox,
        commands,
        control,
        "worker",
        chat_hash=CHAT,
        now=lambda: NOW,
        heartbeat_s=0.005,
        analysis_reports=AnalysisReports(), digest_reports=DigestReports(),
    )
    inbox.persist_batch_and_offset(
        [
            {
                "update_id": 19,
                "operator_hash": OP,
                "command": "liquidate_all",
                "received_at": NOW,
            }
        ],
        20,
    )
    issued = await processor.process_next()
    inbox.persist_batch_and_offset(
        [
            {
                "update_id": 20,
                "operator_hash": OP,
                "command": "confirm",
                "argument_hash": hashlib.sha256(
                    issued.confirmation_token.encode()
                ).hexdigest(),
                "received_at": NOW,
            }
        ],
        21,
    )
    first_claim = inbox.claim_next("dispatcher")

    first = await processor.process_claimed(first_claim)

    assert first.kind == "liquidation_accepted"
    for _ in range(100):
        if commands.intent_status(
            "telegram_command_confirmation_1"
        ) == expected_status:
            break
        await asyncio.sleep(0.005)
    assert commands.intent_status(
        "telegram_command_confirmation_1"
    ) == expected_status
    persisted, _ = commands.intent_for_update(20)
    assert persisted.terminal_reason == expected_terminal_reason
    assert inbox.release(20, "dispatcher", first_claim.version)

    retry_claim = inbox.claim_next("dispatcher")
    retry = await processor.process_claimed(retry_claim)

    assert retry.kind == expected_status
    assert retry.response_text.startswith(expected_prefix)
    assert trading.request_calls == 1
    assert trading.reconcile_calls >= 1


@pytest.mark.parametrize(
    (
        "reconcile_outcome",
        "expected_status",
        "expected_terminal_reason",
        "expected_prefix",
    ),
    [
        (
            LiquidationResult(
                "succeeded",
                False,
                None,
                reason=LiquidationReason.COMPLETED,
            ),
            "succeeded",
            "unmanaged_balance",
            "⚠️ 관리 포지션 청산 완료",
        ),
        (
            LiquidationResult(
                "needs_attention",
                False,
                None,
                reason=LiquidationReason.COMPLETED,
            ),
            "needs_attention",
            None,
            "🚨 처리 결과를 확인해야 합니다",
        ),
    ],
    ids=["valid-unmanaged", "damaged-terminal-reason"],
)
@pytest.mark.anyio
async def test_operations_control_unknown복구는_미관리잔고결과를_재물질화한다(
    tmp_path,
    reconcile_outcome,
    expected_status,
    expected_terminal_reason,
    expected_prefix,
):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'unknown-unmanaged.db'}"
    )
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    unused = LiquidationResult(
        "needs_attention",
        False,
        None,
        reason=LiquidationReason.UNKNOWN_INTENT,
    )
    control, trading = operations_control(
        unused,
        reconcile_outcome,
    )
    processor = CommandProcessor(
        inbox,
        commands,
        control,
        "worker",
        chat_hash=CHAT,
        now=lambda: NOW,
        analysis_reports=AnalysisReports(), digest_reports=DigestReports(),
    )
    issued = commands.issue_confirmation(
        OP,
        CHAT,
        "liquidate_all",
        "fingerprint",
    )
    digest = hashlib.sha256(issued.raw_token.encode()).hexdigest()
    inbox.persist_batch_and_offset(
        [
            {
                "update_id": 21,
                "operator_hash": OP,
                "command": "confirm",
                "argument_hash": digest,
                "received_at": NOW,
            }
        ],
        22,
    )
    intent = commands.consume_and_create_intent(
        digest,
        OP,
        CHAT,
        "liquidate_all",
        "fingerprint",
        NOW,
        update_id=21,
        targets=[
            {"position_id": 7, "symbol": "005930", "quantity": 3}
        ],
    )
    claimed = commands.claim_intent_by_id(intent.id, "old-worker")
    assert commands.mark_running(
        intent.id,
        "old-worker",
        claimed.version,
    )
    assert commands.mark_unknown(
        intent.id,
        "old-worker",
        claimed.version + 1,
    )

    reconciled = await processor.reconcile_unknown()

    assert reconciled.kind == expected_status
    assert commands.intent_status(intent.id) == expected_status
    persisted, _ = commands.intent_for_update(21)
    assert persisted.terminal_reason == expected_terminal_reason
    claim = inbox.claim_next("dispatcher")
    rematerialized = await processor.process_claimed(claim)
    assert rematerialized.kind == expected_status
    assert rematerialized.response_text.startswith(expected_prefix)
    assert trading.request_calls == 0
    assert trading.reconcile_calls == 1


@pytest.mark.anyio
async def test_미지청산status는_monitor를시작하지않고_needs_attention으로종결한다(
        processor):
    processor, inbox, commands, _ = processor
    processor._control.liquidation_status = "brand_new"
    processor._control.liquidation_reason = LiquidationReason.COMPLETED
    inbox.persist_batch_and_offset([
        {"update_id": 15, "operator_hash": OP, "command": "liquidate_all",
         "received_at": NOW}
    ], 16)
    issued = await processor.process_next()
    inbox.persist_batch_and_offset([
        {"update_id": 16, "operator_hash": OP, "command": "confirm",
         "argument_hash": hashlib.sha256(
             issued.confirmation_token.encode()).hexdigest(),
         "received_at": NOW}
    ], 17)

    result = await processor.process_next()

    assert result.kind == "needs_attention"
    assert commands.intent_status(
        "telegram_command_confirmation_1"
    ) == "needs_attention"
    assert processor._accepted_monitors == {}


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
    control.liquidation_reason = LiquidationReason.ACCEPTED
    control.reconcile_outcomes = [
        ("in_progress", LiquidationReason.POSITION_REMAINS),
        ("in_progress", LiquidationReason.POSITION_REMAINS),
        ("needs_attention", LiquidationReason.POSITION_REMAINS),
    ]
    worker = CommandProcessor(inbox, commands, control, "worker",
                                  chat_hash=CHAT, now=lambda: NOW,
                              execution_lease_s=1, heartbeat_s=0.005,
                              analysis_reports=AnalysisReports(),
                              digest_reports=DigestReports())
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
async def test_청산accepted_reason손상은_주의응답후_재실행없이monitor한다(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'accepted-damaged.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    control = Control([], commands)
    control.liquidation_status = "accepted"
    control.liquidation_reason = None
    control.reconcile_outcomes = [
        ("needs_attention", LiquidationReason.POSITION_REMAINS)
    ]
    worker = CommandProcessor(
        inbox,
        commands,
        control,
        "worker",
        chat_hash=CHAT,
        now=lambda: NOW,
        execution_lease_s=1,
        heartbeat_s=0.005,
        analysis_reports=AnalysisReports(), digest_reports=DigestReports(),
    )
    inbox.persist_batch_and_offset([
        {"update_id": 45, "operator_hash": OP, "command": "liquidate_all",
         "received_at": NOW}
    ], 46)
    confirmation = await worker.process_next()
    inbox.persist_batch_and_offset([
        {"update_id": 46, "operator_hash": OP, "command": "confirm",
         "argument_hash": hashlib.sha256(
             confirmation.confirmation_token.encode()).hexdigest(),
         "received_at": NOW}
    ], 47)

    result = await worker.process_next()

    assert result.kind == "needs_attention"
    assert result.response_text.startswith("🚨 처리 결과를 확인해야 합니다")
    assert len(control.liquidate_calls) == 1
    for _ in range(20):
        if commands.intent_status(
                "telegram_command_confirmation_1") != "running":
            break
        await asyncio.sleep(0.005)
    assert commands.intent_status(
        "telegram_command_confirmation_1"
    ) == "needs_attention"
    assert len(control.liquidate_calls) == 1


@pytest.mark.anyio
async def test_청산monitor의_불일치성공도_needs_attention으로종결한다(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'monitor-gate.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    control = Control([], commands)
    control.liquidation_status = "accepted"
    control.liquidation_reason = LiquidationReason.ACCEPTED
    control.reconcile_outcomes = [("succeeded", None)]
    worker = CommandProcessor(
        inbox,
        commands,
        control,
        "worker",
        chat_hash=CHAT,
        now=lambda: NOW,
        execution_lease_s=1,
        heartbeat_s=0.005,
        analysis_reports=AnalysisReports(), digest_reports=DigestReports(),
    )
    inbox.persist_batch_and_offset([
        {"update_id": 43, "operator_hash": OP, "command": "liquidate_all",
         "received_at": NOW}
    ], 44)
    confirmation = await worker.process_next()
    inbox.persist_batch_and_offset([
        {"update_id": 44, "operator_hash": OP, "command": "confirm",
         "argument_hash": hashlib.sha256(
             confirmation.confirmation_token.encode()).hexdigest(),
         "received_at": NOW}
    ], 45)

    result = await worker.process_next()
    for _ in range(20):
        if commands.intent_status(
                "telegram_command_confirmation_1") != "running":
            break
        await asyncio.sleep(0.005)

    assert result.kind == "liquidation_accepted"
    assert commands.intent_status(
        "telegram_command_confirmation_1"
    ) == "needs_attention"


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
async def test_running청산복구의_불일치성공도_needs_attention으로종결한다(
        processor):
    processor, inbox, commands, _ = processor
    issued = commands.issue_confirmation(
        OP, CHAT, "liquidate_all", "fingerprint"
    )
    digest = hashlib.sha256(issued.raw_token.encode()).hexdigest()
    inbox.persist_batch_and_offset([
        {"update_id": 73, "operator_hash": OP, "command": "confirm",
         "argument_hash": digest, "received_at": NOW}
    ], 74)
    intent = commands.consume_and_create_intent(
        digest,
        OP,
        CHAT,
        "liquidate_all",
        "fingerprint",
        NOW,
        update_id=73,
        targets=[
            {"position_id": 7, "symbol": "005930", "quantity": 3}
        ],
    )
    claimed = commands.claim_intent_by_id(intent.id, "old-worker")
    assert commands.mark_running(intent.id, "old-worker", claimed.version)
    assert commands.mark_unknown(
        intent.id, "old-worker", claimed.version + 1
    )
    processor._control.reconcile_outcomes = [("succeeded", None)]

    result = await processor.reconcile_unknown()

    assert result.kind == "needs_attention"
    assert commands.intent_status(intent.id) == "needs_attention"
    assert processor._control.liquidate_calls == []


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
    ("command", "expected", "prefix"),
    [
        ("status", "status", "⚠️ 시스템 주의"),
        ("account", "account", "💰 계좌 요약"),
        ("positions", "positions", "📦 관리 포지션"),
        ("pause", "pause", "✅ 완료"),
        ("stop", "stop", "✅ 완료"),
        ("resume", "confirmation_required", "⚠️ 확인 필요"),
        ("liquidate_all", "confirmation_required", "⚠️ 확인 필요"),
        ("help", "help", "🤖 OhMyStock 명령어"),
    ],
)
async def test_지원명령은_명시된_결과로_수렴(
    processor, command, expected, prefix
):
    processor, inbox, _, _ = processor
    inbox.persist_batch_and_offset([
        {"update_id": 100, "operator_hash": OP, "command": command, "received_at": NOW}
    ], 101)

    result = await processor.process_next()

    assert result.kind == expected
    assert result.response_text.startswith(prefix)
    if command in {"account", "positions"}:
        assert result.outbox_sensitive is True


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("command", "expected_call"),
    [
        ("status", "status"),
        ("account", "account"),
        ("positions", "positions"),
        ("help", "help"),
        ("pause", "control_result"),
        ("stop", "control_result"),
    ],
)
async def test_조회와제어결과는_주입된프레젠터를통과한다(
    tmp_path, command, expected_call
):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / f'{command}.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    presenter = PresenterSpy()
    control = Control([], commands)
    worker = CommandProcessor(
        inbox,
        commands,
        control,
        "worker",
        chat_hash=CHAT,
        now=lambda: NOW,
        presenter=presenter,
        analysis_reports=AnalysisReports(), digest_reports=DigestReports(),
    )
    inbox.persist_batch_and_offset(
        [
            {
                "update_id": 110,
                "operator_hash": OP,
                "command": command,
                "received_at": NOW,
            }
        ],
        111,
    )

    result = await worker.process_next()

    assert result.response_text.startswith("presented:")
    assert presenter.calls[0][0] == expected_call


@pytest.mark.anyio
@pytest.mark.parametrize("command", ["resume", "liquidate_all"])
async def test_위험명령확인은_전체confirm명령과함께프레젠터를통과한다(
    tmp_path, command
):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / f'{command}.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    presenter = PresenterSpy()
    worker = CommandProcessor(
        inbox,
        commands,
        Control([], commands),
        "worker",
        chat_hash=CHAT,
        now=lambda: NOW,
        presenter=presenter,
        analysis_reports=AnalysisReports(), digest_reports=DigestReports(),
    )
    inbox.persist_batch_and_offset(
        [
            {
                "update_id": 120,
                "operator_hash": OP,
                "command": command,
                "received_at": NOW,
            }
        ],
        121,
    )

    result = await worker.process_next()

    call = presenter.calls[0]
    assert call[0] == "confirmation"
    assert call[1].value == command
    assert call[2] == f"/confirm {result.confirmation_token}"
    assert result.response_text == f"presented:confirmation:{command}"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("command", "expected_status"),
    [("resume", "succeeded"), ("liquidate_all", "needs_attention")],
)
async def test_확인소비후위험명령결과도_프레젠터를통과한다(
    tmp_path, command, expected_status
):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / f'{command}.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    presenter = PresenterSpy()
    worker = CommandProcessor(
        inbox,
        commands,
        Control([], commands),
        "worker",
        chat_hash=CHAT,
        now=lambda: NOW,
        presenter=presenter,
        analysis_reports=AnalysisReports(), digest_reports=DigestReports(),
    )
    inbox.persist_batch_and_offset(
        [
            {
                "update_id": 130,
                "operator_hash": OP,
                "command": command,
                "received_at": NOW,
            }
        ],
        131,
    )
    issued = await worker.process_next()
    inbox.persist_batch_and_offset(
        [
            {
                "update_id": 131,
                "operator_hash": OP,
                "command": "confirm",
                "argument_hash": hashlib.sha256(
                    issued.confirmation_token.encode()
                ).hexdigest(),
                "received_at": NOW,
            }
        ],
        132,
    )

    result = await worker.process_next()

    assert result.response_text.startswith("presented:control:")
    assert presenter.calls[-1] == (
        "control_result",
        CommandKind(command),
        expected_status,
        True,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("command", "terminal_status"),
    [("stop", "succeeded"), ("pause", "needs_attention")],
)
async def test_기존terminal은_프레젠터로재표시하고_제어를재실행하지않는다(
    tmp_path, command, terminal_status
):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / f'{command}.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    inbox.persist_batch_and_offset(
        [
            {
                "update_id": 140,
                "operator_hash": OP,
                "command": command,
                "received_at": NOW,
            }
        ],
        141,
    )
    intent = commands.create_intent_for_update(140, command)
    claimed = commands.claim_intent_by_id(intent.id, "old-worker")
    assert commands.mark_running(intent.id, "old-worker", claimed.version)
    assert commands.mark_terminal(
        intent.id, "old-worker", claimed.version + 1, terminal_status
    )
    presenter = PresenterSpy()
    control = Control([], commands)
    worker = CommandProcessor(
        inbox,
        commands,
        control,
        "worker",
        chat_hash=CHAT,
        now=lambda: NOW,
        presenter=presenter,
        analysis_reports=AnalysisReports(), digest_reports=DigestReports(),
    )

    result = await worker.process_next()

    assert result.response_text == f"presented:existing:{command}:{terminal_status}"
    assert presenter.calls == [
        (
            "existing_result",
            CommandKind(command),
            terminal_status,
        )
    ]
    assert control.calls_by_kind == []


@pytest.mark.anyio
async def test_유효하지않은confirm도_프레젠터의기존결과경계를사용한다(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'invalid.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    presenter = PresenterSpy()
    worker = CommandProcessor(
        inbox,
        commands,
        Control([], commands),
        "worker",
        chat_hash=CHAT,
        now=lambda: NOW,
        presenter=presenter,
        analysis_reports=AnalysisReports(), digest_reports=DigestReports(),
    )
    inbox.persist_batch_and_offset(
        [
            {
                "update_id": 150,
                "operator_hash": OP,
                "command": "confirm",
                "argument_hash": "0" * 64,
                "received_at": NOW,
            }
        ],
        151,
    )

    result = await worker.process_next()

    assert result.kind == "confirmation_invalid"
    assert result.response_text == "presented:existing:confirm:confirmation_invalid"
    assert presenter.calls[-1][0] == "existing_result"


@pytest.mark.anyio
async def test_프레젠터예외는_적용된stop_intent를_unknown으로바꾸지않는다(
    tmp_path, caplog
):
    class RaisingPresenter(PresenterSpy):
        def control_result(self, kind, status, *, applied=True):
            raise RuntimeError("presentation-only failure")

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'fallback.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    control = Control([], commands)
    worker = CommandProcessor(
        inbox,
        commands,
        control,
        "worker",
        chat_hash=CHAT,
        now=lambda: NOW,
        presenter=RaisingPresenter(),
        analysis_reports=AnalysisReports(), digest_reports=DigestReports(),
    )
    inbox.persist_batch_and_offset(
        [
            {
                "update_id": 160,
                "operator_hash": OP,
                "command": "stop",
                "received_at": NOW,
            }
        ],
        161,
    )

    result = await worker.process_next()

    assert result.kind == "stop"
    assert result.response_text.startswith("🚨 명령 결과 응답을 표시하지 못했습니다")
    assert commands.intent_status("telegram_command_update_160") == "succeeded"
    assert len(control.calls) == 2
    presentation_logs = [
        record.getMessage()
        for record in caplog.records
        if "telegram command presentation failed" in record.getMessage()
    ]
    assert presentation_logs == [
        (
            "telegram command presentation failed "
            "method=control_result exception_type=RuntimeError"
        )
    ]


@pytest.mark.anyio
async def test_falsey프레젠터도_명시적주입값을그대로사용한다(tmp_path):
    class FalseyPresenter(PresenterSpy):
        def __bool__(self):
            return False

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'falsey.db'}")
    Base.metadata.create_all(engine)
    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    presenter = FalseyPresenter()
    worker = CommandProcessor(
        inbox,
        commands,
        Control([], commands),
        "worker",
        chat_hash=CHAT,
        now=lambda: NOW,
        presenter=presenter,
        analysis_reports=AnalysisReports(), digest_reports=DigestReports(),
    )
    inbox.persist_batch_and_offset(
        [
            {
                "update_id": 170,
                "operator_hash": OP,
                "command": "help",
                "received_at": NOW,
            }
        ],
        171,
    )

    result = await worker.process_next()

    assert result.response_text == "presented:help"
    assert presenter.calls == [("help",)]


@pytest.mark.parametrize(
    ("status", "account_empty", "warning", "reason", "expected"),
    [
        (
            "needs_attention",
            False,
            "market is closed; no sell order placed; manual action required",
            LiquidationReason.MARKET_CLOSED,
            "unavailable_market_closed",
        ),
        (
            "needs_attention",
            False,
            "005930: 장마감 미종결; manual action required",
            LiquidationReason.MARKET_CLOSE_INCOMPLETE,
            "unavailable_market_close_incomplete",
        ),
        (
            "needs_attention",
            False,
            "005930: 거래정지; no sell order placed",
            LiquidationReason.TRADING_HALT,
            "unavailable_trading_halt",
        ),
        (
            "needs_attention",
            False,
            "target sell open orders; ownership unknown",
            LiquidationReason.OPEN_SELL_ORDERS,
            "unavailable_open_orders",
        ),
        (
            "needs_attention",
            False,
            "confirmed target state changed; no sell order placed",
            LiquidationReason.TARGET_STATE_CHANGED,
            "unavailable_state_changed",
        ),
        (
            "needs_attention",
            False,
            "005930: DB수량=9, broker수량=8 불일치",
            LiquidationReason.QUANTITY_MISMATCH,
            "unavailable_quantity_mismatch",
        ),
        (
            "needs_attention",
            False,
            "another managed liquidation intent is active",
            LiquidationReason.ANOTHER_INTENT_ACTIVE,
            "unavailable_intent_active",
        ),
        (
            "needs_attention",
            False,
            "db diagnostic",
            LiquidationReason.PERSISTENCE_FAILED,
            "unavailable_persistence",
        ),
        (
            "needs_attention",
            False,
            "preflight internal diagnostic",
            LiquidationReason.PREFLIGHT_RECONCILIATION_FAILED,
            "unavailable_preflight_reconciliation",
        ),
        (
            "needs_attention",
            False,
            "post-accept internal diagnostic",
            LiquidationReason.POST_ACCEPT_RECONCILIATION_FAILED,
            "unavailable_post_accept_reconciliation",
        ),
        (
            "needs_attention",
            False,
            "unknown liquidation intent",
            LiquidationReason.UNKNOWN_INTENT,
            "unavailable_unknown_intent",
        ),
        (
            "needs_attention",
            False,
            "005930: 잔량=3, 미체결=0",
            LiquidationReason.POSITION_REMAINS,
            "unavailable_position_remains",
        ),
        (
            "succeeded",
            True,
            None,
            LiquidationReason.COMPLETED,
            "succeeded",
        ),
        (
            "succeeded",
            False,
            "no managed liquidation targets; no-op",
            LiquidationReason.NO_TARGETS,
            "succeeded_no_targets",
        ),
        (
            "succeeded",
            False,
            "계좌 전체 잔고 0 아님: 미관리 잔고 존재",
            LiquidationReason.UNMANAGED_BALANCE,
            "succeeded_balance_remains",
        ),
    ],
)
def test_청산reason은_원문대신고정allowlist상태로표시한다(
    status, account_empty, warning, reason, expected
):
    outcome = LiquidationResult(status, account_empty, warning, reason=reason)

    assert CommandProcessor._liquidation_presentation_status(outcome) == expected


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("succeeded", None),
        ("succeeded", LiquidationReason.MARKET_CLOSED),
        ("accepted", None),
        ("accepted", LiquidationReason.COMPLETED),
        ("in_progress", LiquidationReason.ACCEPTED),
        ("failed", LiquidationReason.COMPLETED),
        ("brand_new", LiquidationReason.COMPLETED),
    ],
)
def test_청산status_reason누락불일치는_needs_attention으로fail_closed한다(
    status, reason
):
    outcome = LiquidationResult(
        status,
        False,
        "warning must never decide presentation",
        reason=reason,
    )

    assert (
        CommandProcessor._liquidation_presentation_status(outcome)
        == "needs_attention"
    )


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        ("accepted", LiquidationReason.ACCEPTED, "accepted"),
        (
            "in_progress",
            LiquidationReason.ALREADY_ACCEPTED,
            "in_progress",
        ),
        (
            "in_progress",
            LiquidationReason.POSITION_REMAINS,
            "in_progress",
        ),
    ],
)
def test_청산진행status_reason허용조합은_그대로표시한다(
    status, reason, expected
):
    outcome = LiquidationResult(status, False, None, reason=reason)

    assert CommandProcessor._liquidation_presentation_status(outcome) == expected


def test_청산warning문구는_reason없이는_표시분류에사용하지않는다():
    outcome = LiquidationResult(
        "needs_attention",
        False,
        "market is closed; 005930; DB수량=9; broker secret",
    )

    assert (
        CommandProcessor._liquidation_presentation_status(outcome)
        == "needs_attention"
    )
