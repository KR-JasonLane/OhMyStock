"""트레이딩 관측성(P6 Task 7c, 결정 #36) — 판정·방어선이 로그와 DB에
남는지 고정.

배경(2026-07-24 7b 실환경 관찰): 진입 재시도 판정이 warnings 리스트에만
쌓여 ① 로그에 0건이라 grep 재구성 불가, ② run 종료와 함께 완전 소실돼
"그날 왜 안 샀나"를 SQL로 물을 수 없었다. 결정 #36의 두 요구(상세 로그·
분석 친화 적재)가 트레이딩 진입 경로에서 동시에 깨져 있던 상태."""

import asyncio
import logging
import threading
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine

from app.domain.broker import (Balance, Deposit, MarketData, OpenOrder,
                               OrderAck, OrderSide, Position, Quote)
from app.domain.trading.config import TradingConfig
from app.domain.trading.entry import EntryOutcome
from app.domain.trading.models import (EntryPhase, ExitReason, PositionState,
                                       TradePosition)
from app.domain.trading.monitor import ExitAction, PositionMonitor
from app.domain.trading.reconcile import DbPosition, ReconcileKind
from app.domain.trading.selection import EntryPlan
from app.domain.trading.service import TradingService, _await_terminal
from app.store.models import Base
from app.store.trading_store import TradingStore

KST = timezone(timedelta(hours=9))
T0 = datetime(2026, 7, 24, 0, 10, tzinfo=timezone.utc)   # 09:10 KST

CFG = TradingConfig(max_single_order_krw=100_000_000, max_daily_orders=100,
                    max_daily_order_krw=500_000_000,
                    min_avg_trading_value_krw=0,
                    limit_order_timeout_sec=3.0, exit_limit_timeout_sec=3.0,
                    poll_interval_sec=1.0, quote_failure_threshold=2)


@pytest.fixture(autouse=True)
def _trading_loggers_enabled():
    """alembic 마이그레이션 테스트의 fileConfig(disable_existing_loggers
    기본 True)가 ini에 없는 기존 로거를 세션 내내 비활성화한다 — 실행
    순서에 따라 caplog가 아무것도 못 잡는 현상(test_api_security.py의
    동일 픽스처 참고). 이 모듈이 검증하는 두 로거만 재활성화."""
    for name in ("app.domain.trading.service", "app.domain.trading.monitor"):
        logging.getLogger(name).disabled = False


class _Cal:
    """첫 사이클만 장중 — 진입 판정 1회 수행 후 루프 정상 종료."""

    KST = KST

    def __init__(self, hours=None):
        self._hours = list(hours if hours is not None else [True, False])

    def is_trading_day(self, d) -> bool:
        return True

    def is_market_hours(self, now) -> bool:
        return self._hours.pop(0) if self._hours else False

    def held_business_days(self, entry_date, now) -> int:
        return 0


class _Broker:
    async def get_quotes(self, symbols):
        q = Quote(symbol="005930", name="삼성전자", price=100_000,
                  change_rate=Decimal("0"), volume=0)
        return [MarketData(quote=q, bid=99_900, ask=100_100)
                for _ in symbols]

    async def place_order(self, req):
        return OrderAck(order_no="N1", message="ok")

    async def cancel_order(self, order_no, symbol):
        return OrderAck(order_no="C1", message="cancelled")

    async def get_open_orders(self):
        return []

    async def get_balance(self):
        return Balance((), 0, 0)

    async def get_deposit(self):
        return Deposit(total=10_000_000, available=10_000_000)


def _md(price: int) -> MarketData:
    q = Quote(symbol="005930", name="삼성전자", price=price,
              change_rate=Decimal("0"), volume=0)
    return MarketData(quote=q, bid=price - 100, ask=price + 100)


def _pos(**kw) -> TradePosition:
    base = dict(symbol="005930", name="삼성전자", market="kospi",
                state=PositionState.ENTERED, entry_price=100_000,
                quantity=10, peak_price=100_000, trailing_active=False,
                entered_at=T0)
    base.update(kw)
    return TradePosition(**base)


def test_partial_exit_event는_누적과_잔량과_정확도를_보존한다(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'fills.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    entered = _pos()
    position_id = store.create_position(run_id, entered)
    order_id = store.record_order(
        run_id, position_id, order_no="4", symbol="005930", side="sell",
        order_style="market", req_price=0, req_qty=10, status="submitted",
        resp_body={})
    exiting = replace(
        entered, state=PositionState.EXITING,
        exit_reason=ExitReason.STOP_LOSS)
    store.save_position_snapshot_with_fill_event(
        position_id, exiting, order_id=order_id, kind="exit_partial_fill",
        order_qty=10,
        fill_qty=3, cumulative_fill_qty=3, remaining_qty=7,
        avg_fill_price=70_000, price_confidence="estimated",
        remaining_order_state="open")
    event = store.latest_operational_event()
    assert event.payload["remaining_qty"] == 7
    assert event.payload["price_confidence"] == "estimated"
    assert event.payload["remaining_order_state"] == "open"


def test_부분체결_snapshot과_event는_append실패시_함께_rollback된다(
        tmp_path, monkeypatch):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'fill-atomic.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    pending = _pos(state=PositionState.PENDING_ENTRY, quantity=10)
    position_id = store.create_position(run_id, pending)
    order_id = store.record_order(
        run_id, position_id, order_no="B1", symbol="005930", side="buy",
        order_style="limit", req_price=100_000, req_qty=10,
        status="submitted", resp_body={})
    entered = replace(
        pending, state=PositionState.ENTERED, entry_price=100_000,
        peak_price=100_000, quantity=3, entered_at=T0)

    def fail_append(_session, _event):
        raise RuntimeError("append failed")

    monkeypatch.setattr(
        store._notifications, "append_event_in_session", fail_append)

    with pytest.raises(RuntimeError, match="append failed"):
        store.save_position_snapshot_with_fill_event(
            position_id, entered, order_id=order_id,
            kind="entry_partial_fill", order_qty=10, fill_qty=3,
            cumulative_fill_qty=3, remaining_qty=7,
            avg_fill_price=100_000, price_confidence="estimated",
            remaining_order_state="cancelled")

    restored = store.get_position(position_id)
    assert restored is not None
    assert restored.state is PositionState.PENDING_ENTRY
    assert restored.quantity == 10
    with pytest.raises(LookupError, match="no operational event"):
        store.latest_operational_event()


def test_fill_event장애_state_only폴백은_snapshot과_audit_gap을_함께_남긴다(
        tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'audit-gap.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    pending = _pos(state=PositionState.PENDING_ENTRY, quantity=10)
    position_id = store.create_position(run_id, pending)
    entered = replace(
        pending, state=PositionState.ENTERED, quantity=3, entered_at=T0)

    store.save_position_snapshot_with_audit_gap(
        position_id, entered, gap_kind="entry_fill_event_unavailable",
        unmanaged_qty=2)
    restored = store.get_position(position_id)
    assert restored is not None
    assert restored.state is PositionState.ENTERED
    assert restored.quantity == 3
    assert "[audit-gap] entry_fill_event_unavailable" in (
        store.latest_run()["warnings"])
    assert store.unmanaged_quantity(position_id) == 2

    store.finish_run(run_id, "succeeded", warnings="ordinary warning")
    warnings = store.latest_run()["warnings"]
    assert "[audit-gap] entry_fill_event_unavailable" in warnings
    assert "ordinary warning" in warnings


@pytest.mark.anyio
async def test_service_진입부분체결_callback은_잔고검증전_event를_보류한다(
        tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'fill-service.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    pending = _pos(state=PositionState.PENDING_ENTRY, quantity=10)
    position_id = store.create_position(run_id, pending)
    order_id = store.record_order(
        run_id, position_id, order_no="B1", symbol="005930", side="buy",
        order_style="limit", req_price=100_000, req_qty=10,
        status="submitted", resp_body={})
    entered = replace(
        pending, state=PositionState.ENTERED, entry_price=100_000,
        peak_price=100_000, quantity=3, entered_at=T0)
    broker = _Broker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0,
        run_environment="mock")
    service._on_accepted()
    service._run_id = run_id
    service._pos_ids["005930"] = position_id
    service._order_ids["B1"] = order_id

    await service._record_fill_observation(entered, {
        "kind": "entry_partial_fill", "order_no": "B1", "order_qty": 10,
        "fill_qty": 3, "cumulative_fill_qty": 3, "remaining_qty": 7,
        "avg_fill_price": 100_000, "price_confidence": "estimated",
        "remaining_order_state": "cancelled"})

    restored = store.get_position(position_id)
    assert restored is not None
    assert restored.state is PositionState.PENDING_ENTRY
    assert restored.quantity == 10
    with pytest.raises(LookupError, match="no operational event"):
        store.latest_operational_event()
    assert "005930" in service._entry_fill_observations


@pytest.mark.anyio
async def test_kt00018_계좌합계는_단일진입주문_fill_qty로_귀속하지않는다(
        tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'entry-total.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    pending = _pos(state=PositionState.PENDING_ENTRY, quantity=10,
                   entered_at=None)
    position_id = store.create_position(run_id, pending)
    order_id = store.record_order(
        run_id, position_id, order_no="B1", symbol="005930", side="buy",
        order_style="limit", req_price=100_000, req_qty=10,
        status="submitted", resp_body={})

    class AccountTotalBroker(_Broker):
        quantity = 5

        async def get_balance(self):
            # 수동/고아 2 + 이번 주문 실제 3 = 계좌 종목 합계 5.
            return Balance((
                Position("005930", "삼성전자", self.quantity,
                         99_000, 100_000, self.quantity * 100_000),
            ), 0, 0)

    broker = AccountTotalBroker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0)
    service._on_accepted()
    service._run_id = run_id
    service._pos_ids["005930"] = position_id
    service._order_ids["B1"] = order_id
    entered = replace(
        pending, state=PositionState.ENTERED, quantity=3,
        entry_price=100_000, peak_price=100_000, entered_at=T0)
    await service._record_fill_observation(entered, {
        "kind": "entry_partial_fill", "order_no": "B1", "order_qty": 10,
        "fill_qty": 3, "cumulative_fill_qty": 3, "remaining_qty": 7,
        "avg_fill_price": 100_000, "price_confidence": "estimated",
        "remaining_order_state": "cancelled"})
    await service._apply_entry_outcome(
        EntryPlan("005930", "삼성전자", "kospi", 10, 1_000_000),
        position_id, EntryOutcome(entered, order_no="B1"))

    event = store.latest_operational_event()
    assert event.payload["fill_qty"] == 3
    assert event.payload["cumulative_fill_qty"] == 3
    assert event.payload["remaining_qty"] == 7
    assert store.get_position(position_id).quantity == 3
    assert store.get_position(position_id).state is PositionState.EXITING
    assert service._kill_switch_needs_attention is True
    assert any("excess left unmanaged" in warning
               for warning in service.progress().warnings)

    # baseline 뒤 외부 추가매수로 합계가 7이면 전략 체결과 외부 증감을
    # 구분할 수 없으므로 재오픈하지 않고 EXITING을 유지한다.
    store.save_position_snapshot_state_only(
        position_id, replace(store.get_position(position_id),
                             exit_reason=ExitReason.STOP_LOSS))
    broker.quantity = 7
    await service._post_actions([
        ExitAction("005930", ExitReason.STOP_LOSS, PositionState.CLOSED, 3,
                   requires_reconcile=True)])
    unresolved = store.get_position(position_id)
    assert unresolved.state is PositionState.EXITING
    assert service._pos_ids["005930"] == position_id
    assert any("ownership is ambiguous" in warning
               for warning in service.progress().warnings)

    # 외부 baseline 자체가 줄면 차감식이 모호하므로 CLOSED로 만들지 않는다.
    broker.quantity = 1
    await service._post_actions([
        ExitAction("005930", ExitReason.STOP_LOSS, PositionState.CLOSED, 3,
                   requires_reconcile=True)])
    assert store.get_position(position_id).state is PositionState.EXITING
    assert service._pos_ids["005930"] == position_id
    assert any("ownership is ambiguous" in warning
               for warning in service.progress().warnings)

    # 외부2+전략잔량2처럼 보이는 합계4도 외부 증감 상쇄 가능성이 있어
    # 자동 재오픈하지 않는다.
    broker.quantity = 4
    await service._post_actions([
        ExitAction("005930", ExitReason.STOP_LOSS, PositionState.CLOSED, 3,
                   requires_reconcile=True)])
    unresolved = store.get_position(position_id)
    assert unresolved.state is PositionState.EXITING
    assert service._pos_ids["005930"] == position_id
    assert service._kill_switch_needs_attention is True

    # 합계가 과거 외부 baseline과 같아도 외부 수동매도와 전략 부분체결의
    # 상쇄를 배제할 수 없으므로 자동 CLOSED로 확정하지 않는다.
    service._pos_ids["005930"] = position_id
    broker.quantity = 2
    await service._post_actions([
        ExitAction("005930", ExitReason.STOP_LOSS, PositionState.CLOSED, 3,
                   requires_reconcile=True)])
    assert store.get_position(position_id).state is PositionState.EXITING
    assert service._pos_ids["005930"] == position_id

    # 계좌 종목 합계 0만 실제 무보유라 안전하게 CLOSED를 확정할 수 있다.
    broker.quantity = 0
    await service._post_actions([
        ExitAction("005930", ExitReason.STOP_LOSS, PositionState.CLOSED, 3,
                   requires_reconcile=True)])
    assert store.get_position(position_id).state is PositionState.CLOSED
    assert "005930" not in service._pos_ids


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["startup", "mini"])
async def test_unmanaged혼합소유는_startup과_mini대사에서_EXITING격리(
        tmp_path, path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / f'mixed-{path}.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    entered = _pos(quantity=3)
    position_id = store.create_position(run_id, entered)
    order_id = store.record_order(
        run_id, position_id, order_no="B1", symbol="005930", side="buy",
        order_style="limit", req_price=100_000, req_qty=3,
        status="submitted", resp_body={})
    store.save_position_snapshot_with_fill_event(
        position_id, entered, order_id=order_id, kind="entry_filled",
        order_qty=3, fill_qty=3, cumulative_fill_qty=3, remaining_qty=0,
        avg_fill_price=100_000, price_confidence="estimated",
        remaining_order_state="none", unmanaged_qty=2)

    class MixedBroker(_Broker):
        async def get_balance(self):
            return Balance((
                Position("005930", "삼성전자", 5, 100_000, 100_000,
                         500_000),
            ), 0, 0)

    broker = MixedBroker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0)
    service._on_accepted()
    service._run_id = run_id
    if path == "startup":
        await service._reconcile_startup()
    else:
        service._pos_ids["005930"] = position_id
        await service._mini_reconcile("005930")

    unresolved = store.get_position(position_id)
    assert unresolved.state is PositionState.EXITING
    assert unresolved.quantity == 3
    assert service._pos_ids["005930"] == position_id
    assert service._kill_switch_needs_attention is True
    assert any("manual reconciliation required" in warning
               for warning in service.progress().warnings)


@pytest.mark.anyio
async def test_ownership격리_SELL소멸은_잔고0이어도_CLOSED금지(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'quarantine-sell-gone.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    quarantined = _pos(
        state=PositionState.EXITING, quantity=3,
        exit_reason=None)
    position_id = store.create_position(run_id, quarantined)
    broker = _Broker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0)
    service._on_accepted()
    service._run_id = run_id
    service._pos_ids["005930"] = position_id

    await service._post_actions([
        ExitAction(
            "005930", ExitReason.STOP_LOSS, PositionState.CLOSED, 3,
            requires_reconcile=True, ownership_quarantined=True)])

    assert store.get_position(position_id).state is PositionState.EXITING
    assert service._pos_ids["005930"] == position_id
    assert service._kill_switch_needs_attention is True


@pytest.mark.anyio
async def test_unmanaged혼합소유는_cancel후_balance정렬에서도_수량흡수하지않는다(
        tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'mixed-align.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    entered = _pos(quantity=3)
    position_id = store.create_position(run_id, entered)
    order_id = store.record_order(
        run_id, position_id, order_no="B1", symbol="005930", side="buy",
        order_style="limit", req_price=100_000, req_qty=3,
        status="submitted", resp_body={})
    store.save_position_snapshot_with_fill_event(
        position_id, entered, order_id=order_id, kind="entry_filled",
        order_qty=3, fill_qty=3, cumulative_fill_qty=3, remaining_qty=0,
        avg_fill_price=100_000, price_confidence="estimated",
        remaining_order_state="none", unmanaged_qty=2)

    class MixedBroker(_Broker):
        async def get_balance(self):
            return Balance((
                Position("005930", "삼성전자", 5, 100_000, 100_000,
                         500_000),
            ), 0, 0)

    broker = MixedBroker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0)
    service._on_accepted()
    service._run_id = run_id
    service._pos_ids["005930"] = position_id

    await service._align_open_with_balance({"005930"})

    unresolved = store.get_position(position_id)
    assert unresolved.state is PositionState.EXITING
    assert unresolved.quantity == 3
    assert service._kill_switch_needs_attention is True


@pytest.mark.anyio
async def test_cancel후_balance정렬은_잔고0_ENTERED를_CLOSED로_정리한다(
        tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'zero-align.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    entered = _pos(quantity=3)
    position_id = store.create_position(run_id, entered)
    broker = _Broker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0)
    service._on_accepted()
    service._run_id = run_id
    service._pos_ids["005930"] = position_id

    await service._align_open_with_balance({"005930"})

    assert store.get_position(position_id).state is PositionState.CLOSED
    assert "005930" not in service._pos_ids


@pytest.mark.anyio
async def test_cancel후_balance정렬은_취소대상_심볼만_변경한다(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'targeted-align.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    target_id = store.create_position(run_id, _pos(quantity=3))
    untouched = replace(
        _pos(quantity=4), symbol="000660", name="SK하이닉스")
    untouched_id = store.create_position(run_id, untouched)
    broker = _Broker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0)
    service._on_accepted()
    service._run_id = run_id
    service._pos_ids = {
        "005930": target_id, "000660": untouched_id}

    await service._align_open_with_balance({"005930"})

    assert store.get_position(target_id).state is PositionState.CLOSED
    assert store.get_position(untouched_id).state is PositionState.ENTERED
    assert service._pos_ids["000660"] == untouched_id


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("broker_qty", "unmanaged_qty", "expected_state"),
    [(0, 0, PositionState.CLOSED),
     (5, 2, PositionState.EXITING)])
async def test_balance정렬_DBworker는_cancel에도_terminal까지_join한다(
        tmp_path, monkeypatch, broker_qty, unmanaged_qty, expected_state):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / f'align-cancel-{broker_qty}.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    entered = _pos(quantity=3)
    position_id = store.create_position(run_id, entered)
    if unmanaged_qty:
        order_id = store.record_order(
            run_id, position_id, order_no="B1", symbol="005930", side="buy",
            order_style="limit", req_price=100_000, req_qty=3,
            status="submitted", resp_body={})
        store.save_position_snapshot_with_fill_event(
            position_id, entered, order_id=order_id, kind="entry_filled",
            order_qty=3, fill_qty=3, cumulative_fill_qty=3,
            remaining_qty=0, avg_fill_price=100_000,
            price_confidence="estimated", remaining_order_state="none",
            unmanaged_qty=unmanaged_qty)

    class AlignmentBroker(_Broker):
        async def get_balance(self):
            if broker_qty == 0:
                return Balance((), 0, 0)
            return Balance((
                Position("005930", "삼성전자", broker_qty,
                         100_000, 100_000, broker_qty * 100_000),
            ), 0, 0)

    broker = AlignmentBroker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0)
    service._on_accepted()
    service._run_id = run_id
    service._pos_ids["005930"] = position_id

    worker_entered = threading.Event()
    worker_release = threading.Event()
    worker_done = threading.Event()
    original = store.save_position_snapshot

    def blocked_save(*args, **kwargs):
        worker_entered.set()
        assert worker_release.wait(timeout=2)
        try:
            return original(*args, **kwargs)
        finally:
            worker_done.set()

    monkeypatch.setattr(store, "save_position_snapshot", blocked_save)
    task = asyncio.create_task(
        service._align_open_with_balance({"005930"}))
    assert await asyncio.to_thread(worker_entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    worker_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert worker_done.is_set()
    assert store.get_position(position_id).state is expected_state


@pytest.mark.anyio
async def test_PENDNG_ENTRY_주문소멸_잔고0은_유예재조회뒤_양수면_EXITING(
        tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'pending-delay.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    pending = _pos(
        state=PositionState.PENDING_ENTRY, quantity=3, entered_at=None)
    position_id = store.create_position(run_id, pending)
    store.record_order(
        run_id, position_id, order_no="B1", symbol="005930", side="buy",
        order_style="limit", req_price=100_000, req_qty=3,
        status="submitted", resp_body={})

    class DelayedBalanceBroker(_Broker):
        calls = 0

        async def get_balance(self):
            self.calls += 1
            if self.calls < 3:
                return Balance((), 0, 0)
            return Balance((
                Position("005930", "삼성전자", 2, 100_000, 100_000,
                         200_000),
            ), 0, 0)

    broker = DelayedBalanceBroker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0)
    service._on_accepted()
    service._run_id = run_id

    await service._reconcile_startup()

    unresolved = store.get_position(position_id)
    assert broker.calls == 3
    assert unresolved.state is PositionState.EXITING
    assert unresolved.quantity == 3
    assert service._pos_ids["005930"] == position_id
    assert service._kill_switch_needs_attention is True


@pytest.mark.anyio
async def test_PENDING_ENTRY_최초주문결측은_유예뒤_생존주문으로_재판정(
        tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'pending-order-delay.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    pending = _pos(
        state=PositionState.PENDING_ENTRY, quantity=3, entered_at=None,
        entry_phase=EntryPhase.LIMIT_SUBMITTED)

    class DelayedOrderBroker(_Broker):
        calls = 0

        async def get_open_orders(self):
            self.calls += 1
            if self.calls == 1:
                return []
            return [OpenOrder(
                "B1", "005930", OrderSide.BUY, 3, 3, 100_000, "접수")]

        async def get_balance(self):
            # 양수 잔고가 보여도 원 BUY 잔량이 뒤늦게 나타날 수 있다.
            return Balance((
                Position("005930", "삼성전자", 2, 100_000, 100_000,
                         200_000),
            ), 0, 0)

    broker = DelayedOrderBroker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0)
    service._on_accepted()
    applied, open_orders = await service._run_reconcile(
        [DbPosition(pending, ("B1",))], {"005930": pending})

    assert broker.calls == 2
    assert [action.kind for action in applied] == [
        ReconcileKind.RESUME_ENTRY_WATCH]
    assert [order.order_no for order in open_orders] == ["B1"]


@pytest.mark.anyio
async def test_PENDING_ENTRY_미귀속_live_BUY는_terminal금지_EXITING격리(
        tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'unattributed-buy.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    pending = _pos(
        state=PositionState.PENDING_ENTRY, quantity=3, entered_at=None,
        entry_phase=EntryPhase.LIMIT_SUBMITTED)
    position_id = store.create_position(run_id, pending)

    class UnattributedBuyBroker(_Broker):
        async def get_open_orders(self):
            return [OpenOrder(
                "BROKER_ONLY", "005930", OrderSide.BUY,
                3, 3, 100_000, "접수")]

    broker = UnattributedBuyBroker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0)
    service._on_accepted()
    service._run_id = run_id

    await service._reconcile_startup()

    unresolved = store.get_position(position_id)
    assert unresolved.state is PositionState.EXITING
    assert service._pos_ids["005930"] == position_id
    assert service._kill_switch_needs_attention is True
    assert any("unattributed live BUY" in warning
               for warning in service.progress().warnings)


@pytest.mark.anyio
async def test_미귀속_live_BUY_격리영속실패도_run을_fail_closed한다(
        tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'unattributed-persist-fail.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    pending = _pos(
        state=PositionState.PENDING_ENTRY, quantity=3, entered_at=None,
        entry_phase=EntryPhase.LIMIT_SUBMITTED)
    position_id = store.create_position(run_id, pending)

    class UnattributedBuyBroker(_Broker):
        async def get_open_orders(self):
            return [OpenOrder(
                "BROKER_ONLY", "005930", OrderSide.BUY,
                3, 3, 100_000, "접수")]

    def fail_persist(*_args, **_kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(store, "save_position_snapshot", fail_persist)
    broker = UnattributedBuyBroker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0)
    service._on_accepted()
    service._run_id = run_id

    with pytest.raises(
            RuntimeError, match="safety state could not be established"):
        await service._reconcile_startup()

    assert store.get_position(position_id).state is PositionState.PENDING_ENTRY
    assert service._kill_switch_needs_attention is True


@pytest.mark.anyio
async def test_PENDING_ENTRY_창밖취소직전체결은_fresh잔고로_EXITING격리(
        tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'pending-cancel-race.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    pending = _pos(
        state=PositionState.PENDING_ENTRY, quantity=3, entered_at=None,
        entry_phase=EntryPhase.LIMIT_SUBMITTED)
    position_id = store.create_position(run_id, pending)
    store.record_order(
        run_id, position_id, order_no="B1", symbol="005930", side="buy",
        order_style="limit", req_price=100_000, req_qty=3,
        status="submitted", resp_body={})

    class CancelRaceBroker(_Broker):
        cancelled = False
        post_cancel_balance_calls = 0

        async def get_open_orders(self):
            return [OpenOrder(
                "B1", "005930", OrderSide.BUY, 3, 3, 100_000, "접수")]

        async def cancel_order(self, order_no, symbol):
            self.cancelled = True
            return OrderAck(order_no="C1", message="cancelled")

        async def get_balance(self):
            if not self.cancelled:
                return Balance((), 0, 0)
            self.post_cancel_balance_calls += 1
            if self.post_cancel_balance_calls == 1:
                # cancel ack 직후 kt00018에는 아직 체결분 미반영.
                return Balance((), 0, 0)
            return Balance((
                Position("005930", "삼성전자", 3, 100_000, 100_000,
                         300_000),
            ), 0, 0)

    broker = CancelRaceBroker()
    outside_entry_window = T0 + timedelta(hours=1)
    service = TradingService(
        broker, broker, store, CFG, _Cal([False]), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: outside_entry_window)
    service._on_accepted()
    service._run_id = run_id

    await service._reconcile_startup()

    unresolved = store.get_position(position_id)
    assert broker.cancelled is True
    assert broker.post_cancel_balance_calls == 2
    assert unresolved.state is PositionState.EXITING
    assert unresolved.quantity == 3
    assert service._pos_ids["005930"] == position_id
    assert service._kill_switch_needs_attention is True


@pytest.mark.anyio
async def test_취소성공뒤_격리영속실패는_거래run을_fail_closed한다(
        tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'cancel-persist-fail.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    pending = _pos(
        state=PositionState.PENDING_ENTRY, quantity=3, entered_at=None,
        entry_phase=EntryPhase.LIMIT_SUBMITTED)
    position_id = store.create_position(run_id, pending)
    store.record_order(
        run_id, position_id, order_no="B1", symbol="005930", side="buy",
        order_style="limit", req_price=100_000, req_qty=3,
        status="submitted", resp_body={})

    class PersistFailRaceBroker(_Broker):
        cancelled = False

        async def get_open_orders(self):
            return [OpenOrder(
                "B1", "005930", OrderSide.BUY, 3, 3, 100_000, "접수")]

        async def cancel_order(self, order_no, symbol):
            self.cancelled = True
            return OrderAck(order_no="C1", message="cancelled")

        async def get_balance(self):
            return Balance((
                Position("005930", "삼성전자", 3, 100_000, 100_000,
                         300_000),
            ), 0, 0)

    original = store.save_position_snapshot
    persist_calls = 0

    def fail_first(*args, **kwargs):
        nonlocal persist_calls
        persist_calls += 1
        if persist_calls == 1:
            raise RuntimeError("first quarantine write failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "save_position_snapshot", fail_first)
    broker = PersistFailRaceBroker()
    outside_entry_window = T0 + timedelta(hours=1)
    service = TradingService(
        broker, broker, store, CFG, _Cal([False]), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: outside_entry_window)
    service._on_accepted()
    service._run_id = run_id

    with pytest.raises(
            RuntimeError, match="safety state could not be established"):
        await service._reconcile_startup()

    assert broker.cancelled is True
    assert persist_calls == 1
    assert store.get_position(position_id).state is PositionState.PENDING_ENTRY
    assert service._kill_switch_needs_attention is True


@pytest.mark.anyio
async def test_진입BUY_취소실패는_거래run을_fail_closed한다(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'cancel-failed.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    pending = _pos(
        state=PositionState.PENDING_ENTRY, quantity=3, entered_at=None,
        entry_phase=EntryPhase.LIMIT_SUBMITTED)
    position_id = store.create_position(run_id, pending)
    store.record_order(
        run_id, position_id, order_no="B1", symbol="005930", side="buy",
        order_style="limit", req_price=100_000, req_qty=3,
        status="submitted", resp_body={})

    class CancelFailBroker(_Broker):
        async def get_open_orders(self):
            return [OpenOrder(
                "B1", "005930", OrderSide.BUY, 3, 3, 100_000, "접수")]

        async def cancel_order(self, order_no, symbol):
            raise RuntimeError("cancel temporarily unavailable")

    broker = CancelFailBroker()
    outside_entry_window = T0 + timedelta(hours=1)
    service = TradingService(
        broker, broker, store, CFG, _Cal([False]), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: outside_entry_window)
    service._on_accepted()
    service._run_id = run_id

    with pytest.raises(
            RuntimeError, match="safety state could not be established"):
        await service._reconcile_startup()

    assert store.get_position(position_id).state is PositionState.PENDING_ENTRY
    assert service._pos_ids["005930"] == position_id
    assert service._kill_switch_needs_attention is True


@pytest.mark.anyio
async def test_exit_fill지속실패는_audit_gap뒤_같은process에서_재시도한다(
        tmp_path, monkeypatch):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'fill-retry.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    entered = _pos()
    position_id = store.create_position(run_id, entered)
    order_id = store.record_order(
        run_id, position_id, order_no="S1", symbol="005930", side="sell",
        order_style="market", req_price=0, req_qty=10,
        status="submitted", resp_body={})
    exiting = replace(
        entered, state=PositionState.EXITING, quantity=7,
        exit_reason=ExitReason.STOP_LOSS)
    broker = _Broker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0)
    service._on_accepted()
    service._run_id = run_id
    service._pos_ids["005930"] = position_id
    service._order_ids["S1"] = order_id

    original = store._notifications.append_event_in_session
    attempts = 0

    def fail_first_three(session, event):
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise RuntimeError("temporary append failure")
        return original(session, event)

    monkeypatch.setattr(
        store._notifications, "append_event_in_session", fail_first_three)
    first = await service._monitor._audit_partial_fill(
        exiting, "S1", 10, 3, 7, 99_000, "open")
    assert first is False
    second = await service._monitor._audit_partial_fill(
        exiting, "S1", 10, 3, 7, 99_000, "open")
    assert second is True
    assert store.latest_operational_event().payload["cumulative_fill_qty"] == 3


@pytest.mark.anyio
async def test_fill저장중_cancel은_worker와_audit_gap_terminal뒤_전파한다(
        tmp_path, monkeypatch):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'fill-cancel.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    entered = _pos()
    position_id = store.create_position(run_id, entered)
    order_id = store.record_order(
        run_id, position_id, order_no="S1", symbol="005930", side="sell",
        order_style="market", req_price=0, req_qty=10,
        status="submitted", resp_body={})
    exiting = replace(
        entered, state=PositionState.EXITING, quantity=7,
        exit_reason=ExitReason.STOP_LOSS)
    broker = _Broker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0)
    service._on_accepted()
    service._run_id = run_id
    service._pos_ids["005930"] = position_id
    service._order_ids["S1"] = order_id
    entered_worker = threading.Event()
    release_worker = threading.Event()

    def blocked_failure(*args, **kwargs):
        entered_worker.set()
        assert release_worker.wait(timeout=2)
        raise RuntimeError("append unavailable")

    monkeypatch.setattr(
        store, "save_position_snapshot_with_fill_event", blocked_failure)
    task = asyncio.create_task(service._record_fill_observation(exiting, {
        "kind": "exit_partial_fill", "order_no": "S1", "order_qty": 10,
        "fill_qty": 3, "cumulative_fill_qty": 3, "remaining_qty": 7,
        "avg_fill_price": 99_000, "price_confidence": "estimated",
        "remaining_order_state": "open"}))
    assert await asyncio.to_thread(entered_worker.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release_worker.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    restored = store.get_position(position_id)
    assert restored is not None and restored.quantity == 7
    assert "[audit-gap] exit_fill_event_unavailable" in (
        store.latest_run()["warnings"])


@pytest.mark.anyio
async def test_terminal소유task_예외와_cancel경합도_예외를_회수한다(caplog):
    release = asyncio.Event()

    async def fail_terminal():
        await release.wait()
        raise RuntimeError("db-dsn://secret")

    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(_await_terminal(fail_terminal()))
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert "RuntimeError" in caplog.text
    assert "db-dsn" not in caplog.text


@pytest.mark.anyio
async def test_order_id없는_진입audit_gap도_cancel중_terminal을_회수한다(
        tmp_path, monkeypatch):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'entry-gap-cancel.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    pending = _pos(state=PositionState.PENDING_ENTRY, quantity=3,
                   entered_at=None)
    position_id = store.create_position(run_id, pending)

    class FilledBroker(_Broker):
        async def get_balance(self):
            return Balance((
                Position("005930", "삼성전자", 5, 99_000, 100_000, 500_000),
            ), 0, 0)

    broker = FilledBroker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0)
    service._on_accepted()
    service._run_id = run_id
    service._pos_ids["005930"] = position_id
    entered = replace(
        pending, state=PositionState.ENTERED, entry_price=100_000,
        peak_price=100_000, entered_at=T0)
    worker_entered = threading.Event()
    worker_release = threading.Event()
    original = store.save_position_snapshot_with_audit_gap

    def blocked_gap(*args, **kwargs):
        worker_entered.set()
        assert worker_release.wait(timeout=2)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        store, "save_position_snapshot_with_audit_gap", blocked_gap)
    task = asyncio.create_task(service._apply_entry_outcome(
        EntryPlan("005930", "삼성전자", "kospi", 3, 300_000),
        position_id, EntryOutcome(entered, order_no="UNRECORDED")))
    assert await asyncio.to_thread(worker_entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    worker_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert store.get_position(position_id).state is PositionState.ENTERED
    assert "[audit-gap] entry_fill_event_unavailable" in (
        store.latest_run()["warnings"])
    assert store.unmanaged_quantity(position_id) == 2


def test_진입_스냅샷과_operational_event는_같은_transaction이다(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'entry-event.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    pending = _pos(state=PositionState.PENDING_ENTRY)
    position_id = store.create_position(run_id, pending)
    store.record_order(
        run_id, position_id, order_no="B1", symbol="005930", side="buy",
        order_style="market", req_price=0, req_qty=10, status="submitted",
        resp_body={})
    entered = replace(pending, state=PositionState.ENTERED,
                      entry_price=100_100, peak_price=100_100, quantity=10,
                      entered_at=T0)
    store.save_position_snapshot(position_id, entered)
    event = store.latest_operational_event()
    assert event.kind == "entry_filled"
    assert event.payload["price_confidence"] == "estimated"
    assert event.payload["remaining_order_state"] == "none"


def test_킬스위치_요청과_종료는_run_상태와_같이_사건으로_남는다(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'kill-event.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    store.record_stop_request(run_id, "liquidate_all")
    assert store.latest_operational_event().kind == "kill_switch_requested"
    store.finish_run(run_id, "stopped", stopped_by_kill_switch=True,
                     kill_switch_mode="liquidate_all")
    assert store.latest_operational_event().kind == "kill_switch_completed"


def test_킬스위치중_shutdown은_completed가_아닌_needs_attention이다(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'kill-cancel.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    store.record_stop_request(run_id, "liquidate_all")
    store.finish_run(
        run_id, "stopped", stopped_by_kill_switch=True,
        kill_switch_mode="liquidate_all",
        failure_reason="cancelled (shutdown)")
    assert store.latest_operational_event().kind == "kill_switch_needs_attention"


def test_킬스위치요청뒤_process_restart도_needs_attention을_남긴다(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'kill-stale.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}")
    store.record_stop_request(run_id, "liquidate_all")
    assert store.close_stale_runs("mock") == 1
    event = store.latest_operational_event()
    assert event.kind == "kill_switch_needs_attention"
    assert event.payload["failure_kind"] == "kill_switch_before_crash"


# ── ① 판정 로그 (grep 재구성 가능) ──────────────────────────────────────

@pytest.mark.anyio
async def test_진입_판정이_로그로_남는다(tmp_path, caplog):
    """`trade decision:` 한 줄로 grep 가능해야 한다 — 재시도 사유가
    로그에 0건이던 실측 결함(7b)의 회귀."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'obs.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    broker = _Broker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,   # 분석 결과 없음
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0,
        run_environment="mock")
    logging.getLogger("app.domain.trading.service").propagate = True
    with caplog.at_level(logging.WARNING, logger="app.domain.trading.service"):
        await service.run()
    lines = [r.getMessage() for r in caplog.records]
    assert any("trade decision: no analysis result yet" in m for m in lines)


@pytest.mark.anyio
async def test_판정_로그는_중복되지_않는다(tmp_path, caplog):
    """dedup — 재시도 사이클마다 같은 줄이 쌓이면 grep이 무의미해진다."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'obs2.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    broker = _Broker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0,
        run_environment="mock")
    logging.getLogger("app.domain.trading.service").propagate = True
    with caplog.at_level(logging.WARNING, logger="app.domain.trading.service"):
        service._warn_once("dup message")
        service._warn_once("dup message")
    assert sum("dup message" in r.getMessage()
               for r in caplog.records) == 1


# ── ② warnings DB 영속 (SQL 재구성 가능) ───────────────────────────────

@pytest.mark.anyio
async def test_run_종료시_warnings가_DB에_영속된다(tmp_path):
    """`/trade/status` 메모리에만 있던 판정 사유를 trade_runs.warnings로
    (0012) — run 종료 후에도 SQL로 "왜 안 샀나"를 물을 수 있어야 한다."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'obs3.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    broker = _Broker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0,
        run_environment="mock")
    await service.run()
    import sqlite3
    conn = sqlite3.connect(tmp_path / 'obs3.db')
    (saved,) = conn.execute(
        "select warnings from trade_runs order by id desc limit 1").fetchone()
    conn.close()
    assert saved is not None
    assert "no analysis result yet" in saved


@pytest.mark.anyio
async def test_monitor_경고도_함께_영속된다(tmp_path):
    """트레이더 T7c Important — progress()는 두 출처(서비스+monitor)를
    합치는데 finish_run만 서비스 것만 저장하면, 방어선 신뢰성 경고
    (persist:/quote: 실패)가 run 종료 시 조용히 소실된다."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'obs5.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    broker = _Broker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0,
        run_environment="mock")
    service.start()                      # _on_accepted가 monitor 생성
    # monitor.warnings는 내부 dict의 values() 스냅샷을 반환하는 property —
    # 상시 경고를 흉내내려면 내부 dict에 넣는다(실코드 경로와 동일).
    service._monitor._warnings["quote:005930"] = (
        "005930: quote poll failing (3 consecutive)")
    await service.current_task()
    import sqlite3
    conn = sqlite3.connect(tmp_path / 'obs5.db')
    (saved,) = conn.execute(
        "select warnings from trade_runs order by id desc limit 1").fetchone()
    conn.close()
    assert "quote poll failing" in saved          # monitor 출처 포함
    assert "no analysis result yet" in saved      # 서비스 출처도 유지


@pytest.mark.anyio
async def test_경고가_없으면_None이_영속된다(tmp_path):
    """`_collected_warnings()`의 빈 분기 직접 검증(개발자 T7c 델타 —
    절단 테스트가 이 분기를 대신하지 못한다). 장 마감 후 재기동처럼
    진입 판정 자체가 없는 자연스러운 시나리오."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'obs7.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    broker = _Broker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(hours=[False]),   # 첫 틱부터 장 마감
        lambda: None, sleep=lambda _s: asyncio.sleep(0), now=lambda: T0,
        run_environment="mock")
    await service.run()
    assert service._collected_warnings() is None
    import sqlite3
    conn = sqlite3.connect(tmp_path / 'obs7.db')
    (saved,) = conn.execute(
        "select warnings from trade_runs order by id desc limit 1").fetchone()
    conn.close()
    assert saved is None


def test_절단시_잘린_건수를_남긴다(tmp_path):
    """상한 200 초과 시 "전부 다"로 오인하지 않도록 마커(트레이더 Minor)."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'obs6.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    broker = _Broker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0,
        run_environment="mock")
    service._warnings = [f"w{i}" for i in range(250)]
    body = service._collected_warnings()
    assert body.startswith("[50 earlier warnings truncated]")
    assert body.endswith("w249")          # 최신 우선 보존


def test_warnings_없으면_None(tmp_path):
    """빈 문자열이 아니라 NULL — 집계 쿼리에서 "경고 있음"을 IS NOT NULL로
    물을 수 있게."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'obs4.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}", "mock")
    store.finish_run(run_id, "succeeded")
    import sqlite3
    conn = sqlite3.connect(tmp_path / 'obs4.db')
    (saved,) = conn.execute(
        "select warnings from trade_runs where id=?", (run_id,)).fetchone()
    conn.close()
    assert saved is None


# ── ③ 방어선 상태 전이 로그 ────────────────────────────────────────────

@pytest.mark.anyio
async def test_트레일링_활성화가_로그로_남는다(caplog):
    """"손절/트레일링이 왜 그 가격에 발동했나"의 사후 재구성 재료 —
    활성화(되돌릴 수 없는 래치)는 WARNING."""
    persisted = []
    monitor = PositionMonitor(
        _Broker(), CFG, _Cal(), lambda amount, side: None,
        persist_position=persisted.append,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0)
    logging.getLogger("app.domain.trading.monitor").propagate = True
    with caplog.at_level(logging.INFO, logger="app.domain.trading.monitor"):
        # +6% — trailing_activate_pct(5%) 초과 → 래치
        monitor._evaluate(_pos(), _md(106_000), T0)
    lines = [r.getMessage() for r in caplog.records]
    assert any("defense trailing ACTIVATED" in m and "005930" in m
               for m in lines)


@pytest.mark.anyio
async def test_peak_갱신이_로그로_남는다(caplog):
    persisted = []
    monitor = PositionMonitor(
        _Broker(), CFG, _Cal(), lambda amount, side: None,
        persist_position=persisted.append,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0)
    logging.getLogger("app.domain.trading.monitor").propagate = True
    with caplog.at_level(logging.INFO, logger="app.domain.trading.monitor"):
        monitor._evaluate(_pos(), _md(102_000), T0)   # +2% — 래치 전 peak만
    lines = [r.getMessage() for r in caplog.records]
    assert any("defense peak updated" in m for m in lines)
    assert not any("ACTIVATED" in m for m in lines)
