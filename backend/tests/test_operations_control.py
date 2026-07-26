import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.core.background_service import StopMode
from app.core.operations_control import (AccountSnapshotDeferred,
                                         OperationsControl)
from app.domain.broker import Balance, Deposit, Position
from app.domain.trading.models import (
    LiquidationReason,
    LiquidationResult,
    LiquidationTarget,
    PositionState,
    TradePosition,
)

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 7, 24, 10, 0, tzinfo=KST)


class Calendar:
    KST = KST


class Scheduler:
    paused = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def snapshot(self):
        return {"paused": self.paused, "dead": False, "jobs": {}}


class Trading:
    mode = None

    def request_stop(self, mode):
        self.mode = mode

    def stop_requested(self):
        return self.mode

    async def request_stop_once(self, intent_id, mode):
        self.mode = mode
        return True

    async def request_managed_liquidation(self, intent_id, targets, *, expected_run_id=None):
        if not hasattr(self, "managed_results"):
            self.managed_results = {}
        return self.managed_results.get(
            intent_id,
            LiquidationResult(
                "accepted",
                False,
                None,
                reason=LiquidationReason.ACCEPTED,
            ),
        )

    async def reconcile_control_intent(self, intent_id, targets=()):
        self.last_reconcile = (intent_id, tuple(targets))
        return LiquidationResult(
            "needs_attention",
            False,
            f"reconciled:{intent_id}",
            reason=LiquidationReason.POSITION_REMAINS,
        )


class Broker:
    balance_error = None
    deposit_error = None

    async def get_deposit(self):
        await asyncio.sleep(0)
        if self.deposit_error:
            raise self.deposit_error
        return Deposit(1_000_000, 1_000_000)

    async def get_balance(self):
        await asyncio.sleep(0)
        if self.balance_error:
            raise self.balance_error
        return Balance(
            (Position("005930", "삼성전자", 3, 100, 110, 330),
             Position("000660", "SK하이닉스", 2, 200, 210, 420)),
            1_200_000, 20_000)


class Store:
    def realized_pnl_today(self, environment, now):
        return -12_000, "estimated"

    def open_positions(self, environment):
        pos = TradePosition("005930", "삼성전자", "kospi",
                            PositionState.ENTERED, 100, 3, 100, False,
                            entered_at=NOW)
        return [(7, pos)], []


@pytest.fixture
def control():
    return OperationsControl(Scheduler(), Trading(), Store(), Broker(),
                             Calendar(), "mock", now=lambda: NOW)


@pytest.mark.anyio
async def test_resume은_scheduler_pause만_해제한다(control):
    control.scheduler.pause()
    control.trading.request_stop(StopMode.STOP_NEW_ENTRIES)
    await control.resume_scheduler(expected=control.scheduler_fingerprint())
    assert control.scheduler.paused is False
    assert control.trading.stop_requested() is StopMode.STOP_NEW_ENTRIES


@pytest.mark.anyio
async def test_account는_두소스를_병렬조회하고_총수익률을_만들지_않는다(control):
    summary = await control.account_summary()
    assert summary.deposit == 1_000_000
    assert summary.available_deposit == 1_000_000
    assert summary.total_eval == 1_200_000
    assert summary.total_return_rate is None
    assert summary.trading_day.isoformat() == "2026-07-24"


@pytest.mark.anyio
async def test_balance실패에도_deposit과_실현손익을_유지한다(control):
    control.broker.balance_error = RuntimeError("down")
    summary = await control.account_summary()
    assert summary.available_deposit == 1_000_000
    assert summary.total_eval is None
    assert summary.realized_pnl == -12_000
    assert summary.realized_pnl_confidence == "estimated"
    assert summary.failed_fields == ("balance",)


@pytest.mark.anyio
async def test_deposit실패에도_balance와실현손익을_유지한다(control):
    control.broker.deposit_error = RuntimeError("down")
    summary = await control.account_summary()
    assert summary.deposit is None
    assert summary.available_deposit is None
    assert summary.total_eval == 1_200_000
    assert summary.total_profit == 20_000
    assert summary.realized_pnl == -12_000
    assert summary.realized_pnl_confidence == "estimated"
    assert summary.failed_fields == ("deposit",)


@pytest.mark.anyio
async def test_관리분만_preview하고_미관리잔고를_구분한다(control):
    preview = await control.liquidation_preview()
    assert preview.managed_symbols == ("005930",)
    assert preview.unmanaged_symbols == ("000660",)
    assert preview.targets[0].position_id == 7


@pytest.mark.anyio
async def test_digest는_fresh_cache나진행중조회없이_broker호출을_시작하지않는다(
        control):
    with pytest.raises(AccountSnapshotDeferred):
        await control.account_summary(priority="digest")


@pytest.mark.anyio
async def test_digest는_interactive_singleflight를_공유한다(control):
    interactive = asyncio.create_task(control.account_summary())
    await asyncio.sleep(0)
    digest = await control.account_summary(priority="digest")
    assert digest is await interactive


@pytest.mark.anyio
async def test_빈청산대상은_broad_stop으로_승격되지않는다(control):
    result = await control.liquidate_managed("intent-empty", ())
    assert result.status == "succeeded"
    assert result.reason is LiquidationReason.NO_TARGETS
    assert control.trading.stop_requested() is None


@pytest.mark.anyio
async def test_control은_동일intent재조회에서_cached_terminal을_반환한다(control):
    preview = await control.liquidation_preview()
    terminal = LiquidationResult(
        "succeeded",
        False,
        "terminal cached",
        reason=LiquidationReason.COMPLETED,
    )
    control.trading.managed_results = {"same_intent": terminal}
    first = await control.liquidate_managed("same_intent", preview.targets)
    second = await control.liquidate_managed("same_intent", preview.targets)
    assert first.status == second.status == "succeeded"
    assert second.reason is LiquidationReason.UNMANAGED_BALANCE
    assert "terminal cached" in second.warning


@pytest.mark.anyio
async def test_청산대사는_공용control을거쳐_trading에위임한다(control):
    target = LiquidationTarget(7, "005930", 3)
    result = await control.reconcile_control_intent("intent_7", (target,))
    assert result.status == "needs_attention"
    assert result.warning.startswith("reconciled:intent_7")
    assert "미관리 잔고 존재" in result.warning
    assert result.reason is LiquidationReason.POSITION_REMAINS
    assert control.trading.last_reconcile == ("intent_7", (target,))


@pytest.mark.anyio
async def test_청산대사의_balance실패는_terminal로축소하지않는다(control):
    control.broker.balance_error = RuntimeError("down")
    target = LiquidationTarget(7, "005930", 3)

    with pytest.raises(RuntimeError, match="down"):
        await control.reconcile_control_intent("intent_7", (target,))

    assert control.trading.last_reconcile == ("intent_7", (target,))


@pytest.mark.anyio
async def test_trading서비스부재는_구조화된비활성사유를반환한다(control):
    control.trading = None
    target = LiquidationTarget(7, "005930", 3)

    requested = await control.liquidate_managed("intent_8", (target,))
    reconciled = await control.reconcile_control_intent("intent_8", (target,))

    assert requested.reason is LiquidationReason.TRADING_INACTIVE
    assert reconciled.reason is LiquidationReason.TRADING_INACTIVE
