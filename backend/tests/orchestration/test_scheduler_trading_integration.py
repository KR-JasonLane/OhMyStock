"""같은 날 재기동 통합 테스트(P6 계획 Task 6 — 트레이더 계획 리뷰
Important). Task 1·2의 격리 유닛 테스트와 Task 5의 가짜 서비스 테스트
사이 공백 봉합: **실제 SchedulerService의 Decision이 실제 TradingService를
재기동**시키고, 2차 run이 DB에서 OrderCaps/_entries_done을 정확히 시딩해
이중 진입이 발생하지 않음을 실증한다(§5-1이 막으려는 정확한 사고의 결합
경로 — 스케줄러↔서비스 배선 실수까지 잡는다)."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine

from app.domain.broker import (Balance, Deposit, MarketData, OrderAck,
                               Quote)
from app.domain.orchestration.config import ScheduleConfig
from app.domain.orchestration.service import SchedulerService
from app.domain.orchestration.timeline import Action, Job, Reason
from app.domain.trading.config import TradingConfig
from app.domain.trading.service import TradingService
from app.store.analysis_store import AnalysisStore
from app.store.collection_store import CollectionStore
from app.store.models import Base
from app.store.scheduler_store import SchedulerStore
from app.store.scoring_store import ScoringStore
from app.store.trading_store import TradingStore

KST = timezone(timedelta(hours=9))
THU = date(2026, 7, 23)

CFG = TradingConfig(max_single_order_krw=100_000_000, max_daily_orders=100,
                    max_daily_order_krw=500_000_000,
                    min_avg_trading_value_krw=0,
                    limit_order_timeout_sec=3.0, exit_limit_timeout_sec=3.0,
                    poll_interval_sec=1.0, quote_failure_threshold=2)


class _Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class _Cal:
    KST = KST

    def __init__(self):
        self._hours = [True, False]      # 1사이클 후 장 마감 — run 정상 종료

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5

    def is_market_hours(self, now) -> bool:
        return self._hours.pop(0) if self._hours else False

    def held_business_days(self, entry_date, now) -> int:
        return 0


class _Broker:
    """얇은 페이크 — 실 네트워크 없음(보안 계획 리뷰 확인 항목)."""

    def __init__(self):
        self.placed = []

    async def get_quotes(self, symbols):
        q = Quote(symbol="005930", name="삼성전자", price=100_000,
                  change_rate=Decimal("0"), volume=0)
        return [MarketData(quote=q, bid=99_900, ask=100_100)
                for _ in symbols]

    async def place_order(self, req):
        self.placed.append(req)
        return OrderAck(order_no=f"N{len(self.placed)}", message="ok")

    async def cancel_order(self, order_no, symbol):
        return OrderAck(order_no=f"C{order_no}", message="cancelled")

    async def get_open_orders(self):
        return []

    async def get_balance(self):
        return Balance((), 0, 0)

    async def get_deposit(self):
        return Deposit(total=10_000_000, available=10_000_000)


async def _until(cond, timeout_s: float = 3.0) -> bool:
    for _ in range(int(timeout_s / 0.005)):
        if cond():
            return True
        await asyncio.sleep(0.005)
    return False


LATEST = {"picks": [{"symbol": "005930", "rank": 1}],
          "score_reference_date": "2026-07-22"}


@pytest.mark.anyio
async def test_같은_날_재기동을_스케줄러가_트리거하고_시딩이_이중진입을_막는다(
        tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'itg.db'}")
    Base.metadata.create_all(engine)
    clock = _Clock(datetime(2026, 7, 23, 0, 5, tzinfo=timezone.utc))
    trading_store = TradingStore(engine, now=clock)

    # 1차 run: 09:05 기동 → 매수 1건 발주 후 09:20 크래시(failed)
    prior = trading_store.create_run("{}", "mock")
    trading_store.record_order(
        prior, None, order_no="P1", symbol="005930", side="buy",
        order_style="limit", req_price=100_000, req_qty=2, status="filled",
        resp_body={"ord_no": "P1", "return_msg": "ok"}, est_krw=200_000)
    clock.t = datetime(2026, 7, 23, 0, 20, tzinfo=timezone.utc)
    trading_store.finish_run(prior, "failed", failure_reason="crash")

    # 09:30 — 백오프(60초) 경과. 실제 스케줄러 + 실제 트레이딩 서비스 조립
    clock.t = datetime(2026, 7, 23, 0, 30, tzinfo=timezone.utc)
    broker = _Broker()
    trading = TradingService(
        broker, broker, trading_store, CFG, _Cal(), lambda: LATEST,
        sleep=lambda _s: asyncio.sleep(0), now=clock,
        run_environment="mock")
    scheduler_store = SchedulerStore(
        engine, CollectionStore(engine), ScoringStore(engine),
        AnalysisStore(engine), trading_store, run_environment="mock",
        now=clock)
    scheduler = SchedulerService(
        {Job.COLLECT: None, Job.SCORE: None, Job.ANALYZE: None,
         Job.TRADE: trading},
        scheduler_store, ScheduleConfig(), _Cal(),
        sleep=lambda _s: asyncio.sleep(0), now=clock)

    await scheduler._tick()                       # RETRY 판정 → start()
    assert await _until(lambda: not trading.is_running())

    # ① 스케줄러가 재시도로 기동했고 이벤트에 남았다
    assert (Job.TRADE, "retry") in [
        (Job.TRADE, e["action"]) for e in scheduler_store.recent_events()
        if e["job"] == "trade"]
    # ② 2차 run이 DB 시딩으로 한도·진입 게이트를 복원 — 이중 진입 없음
    assert trading._caps.order_count == 1
    assert trading._caps.order_krw == 200_000
    assert trading._entries_done is True
    assert broker.placed == []                    # 신규 발주 0 (진입 배치 스킵)
    # ③ 2차 run 정상 종료 기록
    latest = trading_store.latest_run()
    assert latest["run_id"] != prior
    assert latest["status"] == "succeeded"


@pytest.mark.anyio
async def test_매수_0건_실패_후_재기동은_정상_신규_진입(tmp_path):
    """3번째 결합 시나리오(트레이더 T6 Minor) — 진입 전 크래시(주문 0건)면
    시딩할 것이 없고, 재기동 run이 진입 창 안에서 정상적으로 새 배치를
    수행한다(기회 상실 없음)."""
    from datetime import date as _date

    from app.domain.broker import Position
    from app.store.models import CandleRow, InstrumentRow

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(engine)
    clock = _Clock(datetime(2026, 7, 23, 0, 6, tzinfo=timezone.utc))
    trading_store = TradingStore(engine, now=clock)

    # 진입 재료(entry_context 실조인): 종목 카탈로그 + 신호일 종가
    from sqlalchemy.orm import Session
    with Session(engine) as session:
        session.add(InstrumentRow(symbol="005930", name="삼성전자",
                                  market="kospi", instrument_type="stock",
                                  state="", audit_info="정상", is_active=True,
                                  updated_at=clock()))
        session.add(CandleRow(symbol="005930", date=_date(2026, 7, 22),
                              open=100_000, high=100_000, low=100_000,
                              close=100_000, volume=1_000_000))
        session.commit()

    # 1차 run: 주문 0건인 채 09:06 크래시
    prior = trading_store.create_run("{}", "mock")
    trading_store.finish_run(prior, "failed", failure_reason="crash")

    # 09:20 KST — 백오프 경과 + 진입 창(09:05~09:30) 안
    clock.t = datetime(2026, 7, 23, 0, 20, tzinfo=timezone.utc)
    broker = _Broker()

    async def balance_with_position():
        pos = Position(symbol="005930", name="삼성전자", quantity=9,
                       avg_price=100_050, current_price=100_050,
                       eval_amount=900_450)
        return Balance((pos,), 0, 0)

    broker.get_balance = balance_with_position   # 진입 후 잔고 대사 재료
    trading = TradingService(
        broker, broker, trading_store, CFG, _Cal(), lambda: LATEST,
        sleep=lambda _s: asyncio.sleep(0), now=clock,
        run_environment="mock")
    scheduler_store = SchedulerStore(
        engine, CollectionStore(engine), ScoringStore(engine),
        AnalysisStore(engine), trading_store, run_environment="mock",
        now=clock)
    scheduler = SchedulerService(
        {Job.COLLECT: None, Job.SCORE: None, Job.ANALYZE: None,
         Job.TRADE: trading},
        scheduler_store, ScheduleConfig(), _Cal(),
        sleep=lambda _s: asyncio.sleep(0), now=clock)

    await scheduler._tick()
    assert await _until(lambda: not trading.is_running())
    buys = [r for r in broker.placed if r.side.value == "buy"]
    assert len(buys) == 1                        # 새 진입 배치 정상 수행
    assert trading_store.latest_run()["status"] == "succeeded"


@pytest.mark.anyio
async def test_킬스위치로_멈춘_날은_스케줄러가_재기동하지_않는다(tmp_path):
    """§4-d 비대칭의 결합 실증 — 운영자 의사(킬스위치)는 같은 날 자동
    재기동 대상이 아니다."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'kill.db'}")
    Base.metadata.create_all(engine)
    clock = _Clock(datetime(2026, 7, 23, 0, 5, tzinfo=timezone.utc))
    trading_store = TradingStore(engine, now=clock)
    prior = trading_store.create_run("{}", "mock")
    trading_store.finish_run(prior, "stopped", stopped_by_kill_switch=True,
                             kill_switch_mode="liquidate_all")
    clock.t = datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc)
    broker = _Broker()
    trading = TradingService(
        broker, broker, trading_store, CFG, _Cal(), lambda: LATEST,
        sleep=lambda _s: asyncio.sleep(0), now=clock,
        run_environment="mock")
    scheduler_store = SchedulerStore(
        engine, CollectionStore(engine), ScoringStore(engine),
        AnalysisStore(engine), trading_store, run_environment="mock",
        now=clock)
    scheduler = SchedulerService(
        {Job.COLLECT: None, Job.SCORE: None, Job.ANALYZE: None,
         Job.TRADE: trading},
        scheduler_store, ScheduleConfig(), _Cal(),
        sleep=lambda _s: asyncio.sleep(0), now=clock)
    await scheduler._tick()
    assert trading.is_running() is False          # 기동 안 됨
    snap = scheduler.snapshot()
    assert snap["jobs"]["trade"] == {"action": Action.WAIT.value,
                                     "reason": Reason.COMPLETED.value,
                                     "next_attempt_at": None}
