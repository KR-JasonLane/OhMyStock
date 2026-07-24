import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.core.background_service import StopMode
from app.core.operations_control import (AccountSnapshotDeferred,
                                         OperationsControl)
from app.domain.broker import Balance, Deposit, Position
from app.domain.trading.models import PositionState, TradePosition
from app.domain.trading.models import LiquidationResult

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

    async def request_managed_liquidation(self, intent_id, targets):
        if not hasattr(self, "managed_results"):
            self.managed_results = {}
        return self.managed_results.get(
            intent_id, LiquidationResult("accepted", False, None))


class Broker:
    balance_error = None

    async def get_deposit(self):
        await asyncio.sleep(0)
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
    assert control.trading.stop_requested() is None


@pytest.mark.anyio
async def test_control은_동일intent재조회에서_cached_terminal을_반환한다(control):
    preview = await control.liquidation_preview()
    terminal = LiquidationResult("succeeded", False, "terminal cached")
    control.trading.managed_results = {"same_intent": terminal}
    first = await control.liquidate_managed("same_intent", preview.targets)
    second = await control.liquidate_managed("same_intent", preview.targets)
    assert first.status == second.status == "succeeded"
    assert "terminal cached" in second.warning
