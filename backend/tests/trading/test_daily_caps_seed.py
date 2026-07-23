"""P6 Task 1 — 같은 날 재기동 시 일일 한도 DB 시딩(스펙 §5-1, P5 정정).

OrderCaps는 run 단위 인메모리라 재기동이 "일일" 한도를 리셋한다 — 이
회귀들은 (a) TradingStore.daily_order_usage의 집계 계약(매수·매도 무구분,
환경 필터, KST 날짜 경계, reconcile 취소 감사행 제외, max(req, fills))과
(b) TradingService._seed_daily_caps의 복원(카운터/래치/진입 게이트)을
고정한다."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine

from app.domain.broker import Balance, Deposit, MarketData, OrderAck, Quote
from app.domain.trading.config import TradingConfig
from app.domain.trading.service import TradingService
from app.store.models import Base
from app.store.trading_store import TradingStore

KST = timezone(timedelta(hours=9))
# 2026-07-22(수) 09:10 KST = 00:10 UTC — 진입 창 안. UTC-aware로 저장해
# 프로덕션(Postgres, aware UTC) 날짜 판정 의미론과 동일 경로를 태운다.
T0 = datetime(2026, 7, 22, 0, 10, tzinfo=timezone.utc)
DAY = datetime(2026, 7, 22).date()

CFG = TradingConfig(max_single_order_krw=100_000_000, max_daily_orders=5,
                    max_daily_order_krw=500_000_000,
                    min_avg_trading_value_krw=0,
                    limit_order_timeout_sec=3.0, exit_limit_timeout_sec=3.0,
                    poll_interval_sec=1.0, quote_failure_threshold=2)

_BODY = {"ord_no": "X", "return_msg": "ok"}


class _Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def clock() -> _Clock:
    return _Clock(T0)


@pytest.fixture
def store(tmp_path, clock) -> TradingStore:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'caps.db'}")
    Base.metadata.create_all(engine)
    return TradingStore(engine, now=clock)


def _order(store, run_id, *, side="buy", price=100_000, qty=1,
           status="filled", order_no="ORD1", est=None) -> int:
    return store.record_order(run_id, None, order_no=order_no,
                              symbol="005930", side=side, order_style="limit",
                              req_price=price, req_qty=qty, status=status,
                              resp_body=_BODY,
                              est_krw=price * qty if est is None else est)


# ── store: daily_order_usage 집계 계약 ──────────────────────────────────

def test_주문_없으면_전부_영(store):
    usage = store.daily_order_usage(DAY, "mock")
    assert (usage.order_count, usage.order_krw, usage.has_buy) == (0, 0, False)


def test_매수_매도_무구분_합산과_체결합_반영(store):
    """금액은 주문당 max(est_krw, req 금액, 체결 합) — 시장가(req 0,
    est 미기록 레거시 행)는 체결 합이 폴백. 매도도 건수·금액에 합산
    (check() 누적 의미론)."""
    run_id = store.create_run("{}", "mock")
    _order(store, run_id, side="buy", price=100_000, qty=2)        # req 200k
    sell_id = _order(store, run_id, side="sell", price=0, qty=1,
                     order_no="ORD2", est=0)                       # 레거시 시장가
    store.record_fill(sell_id, fill_price=150_000, fill_qty=1, filled_at=T0)
    usage = store.daily_order_usage(DAY, "mock")
    assert usage.order_count == 2
    assert usage.order_krw == 200_000 + 150_000
    assert usage.has_buy is True


def test_시장가_주문은_est_krw로_계상된다(store):
    """P6-T1 트레이더 Critical 회귀 — record_fill이 프로덕션 미배선이라
    시장가(req_price=0)는 발주 시점 추정 금액(est_krw)이 유일한 원천.
    체결 기록이 전혀 없어도 금액이 0으로 빠지지 않아야 한다."""
    run_id = store.create_run("{}", "mock")
    _order(store, run_id, side="sell", price=0, qty=3, est=450_000)
    usage = store.daily_order_usage(DAY, "mock")
    assert usage.order_krw == 450_000


def test_부분체결_다건이면_체결합이_est를_넘을_때_체결합_채택(store):
    run_id = store.create_run("{}", "mock")
    order_id = _order(store, run_id, side="buy", price=0, qty=4, est=380_000)
    store.record_fill(order_id, fill_price=100_000, fill_qty=2, filled_at=T0)
    store.record_fill(order_id, fill_price=105_000, fill_qty=2, filled_at=T0)
    usage = store.daily_order_usage(DAY, "mock")
    assert usage.order_krw == 410_000  # max(380k, 0, 200k+210k)


def test_매도만_있으면_has_buy_False(store):
    run_id = store.create_run("{}", "mock")
    _order(store, run_id, side="sell", price=100_000, qty=1)
    usage = store.daily_order_usage(DAY, "mock")
    assert usage.order_count == 1
    assert usage.has_buy is False


def test_다른_환경의_주문은_집계_제외(store):
    """리플레이 주문이 모의 한도를 소비하는 교차 오염 차단(§4-1)."""
    replay_run = store.create_run("{}", "replay")
    _order(store, replay_run, side="buy")
    assert store.daily_order_usage(DAY, "mock").order_count == 0
    assert store.daily_order_usage(DAY, "replay").order_count == 1


def test_reconcile_취소_감사행은_제외_발주후_취소는_포함(store):
    """생성 시점부터 cancelled+updated_at NULL인 행은 발주가 아니다
    (_record_reconcile_cancel). 발주 후 취소는 updated_at이 남아 포함."""
    run_id = store.create_run("{}", "mock")
    _order(store, run_id, status="cancelled", order_no="CXL1")  # 감사행
    placed = _order(store, run_id, status="submitted", order_no="ORD2")
    store.update_order_status(placed, "cancelled")              # 발주 후 취소
    usage = store.daily_order_usage(DAY, "mock")
    assert usage.order_count == 1


def test_KST_자정_경계_판정(tmp_path):
    """created_at은 UTC 저장 — 21일 23:50 UTC는 22일 08:50 KST(당일 포함),
    21일 14:00 UTC는 21일 23:00 KST(전일 제외). P6 계획 Task 4 Critical과
    동일 클래스의 경계."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'edge.db'}")
    Base.metadata.create_all(engine)
    clock = _Clock(datetime(2026, 7, 21, 14, 0, tzinfo=timezone.utc))
    store = TradingStore(engine, now=clock)
    run_id = store.create_run("{}", "mock")
    _order(store, run_id, order_no="PREV")       # 21일 23:00 KST — 전일
    clock.t = datetime(2026, 7, 21, 23, 50, tzinfo=timezone.utc)
    _order(store, run_id, order_no="MORN")       # 22일 08:50 KST — 당일
    usage = store.daily_order_usage(DAY, "mock")
    assert usage.order_count == 1


# ── service: _seed_daily_caps 복원 ──────────────────────────────────────

class _SeedStore(TradingStore):
    """entry_context 조인 재료를 스텁 — 수집 테이블 시드 없이 서비스 구동
    (test_service.StoreForTest와 동일 패턴)."""

    def entry_context(self, symbols, signal_date, avg_days=20):
        return {}

    def instrument_state(self, symbol):
        return None


class _Cal:
    KST = KST

    def __init__(self, hours):
        self._hours = list(hours)

    def is_trading_day(self, d) -> bool:
        return True

    def is_market_hours(self, now) -> bool:
        return self._hours.pop(0) if self._hours else False

    def held_business_days(self, entry_date, now) -> int:
        return 0


class _Broker:
    """진입이 게이트되는 시나리오 전용 — 시세/잔고는 최소 표면."""

    def __init__(self):
        self.placed = []

    async def get_quotes(self, symbols):
        q = Quote(symbol="005930", name="삼성전자", price=100_000,
                  change_rate=Decimal("0"), volume=0)
        return [MarketData(quote=q, bid=99_900, ask=100_100)
                for _ in symbols]

    async def place_order(self, req):
        self.placed.append(req)
        return OrderAck(order_no="NEW1", message="ok")

    async def cancel_order(self, order_no, symbol):
        return OrderAck(order_no=f"CXL{order_no}", message="cancelled")

    async def get_open_orders(self):
        return []

    async def get_balance(self):
        return Balance((), 0, 0)

    async def get_deposit(self):
        return Deposit(total=10_000_000, available=10_000_000)


async def _yield_sleep(_):
    await asyncio.sleep(0)


LATEST = {"picks": [{"symbol": "005930", "rank": 1}],
          "score_reference_date": "2026-07-21"}


def _seed_service(tmp_path, prior_orders):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'svc.db'}")
    Base.metadata.create_all(engine)
    store = _SeedStore(engine, now=_Clock(T0))
    if prior_orders:
        run_id = store.create_run("{}", "mock")
        store.finish_run(run_id, "failed", failure_reason="crash")
        for i, (side, price, qty) in enumerate(prior_orders):
            _order(store, run_id, side=side, price=price, qty=qty,
                   order_no=f"P{i}")
    broker = _Broker()
    service = TradingService(broker, broker, store, CFG, _Cal([True, False]),
                             lambda: LATEST, sleep=_yield_sleep,
                             now=lambda: T0)
    return service, store, broker


@pytest.mark.anyio
async def test_당일_매수_존재시_카운터_복원과_진입_배치_게이트(tmp_path):
    service, store, broker = _seed_service(
        tmp_path, [("buy", 100_000, 2), ("sell", 0, 1)])
    await service.run()
    assert service._caps.order_count == 2
    assert service._caps.order_krw == 200_000  # 매도는 est 미기록(0) 가정 행
    assert service._caps.buy_blocked is False  # 상한 미달
    assert broker.placed == []                 # 진입 배치 게이트(이중 진입 방지)
    progress = service.progress()
    assert any("daily caps seeded" in w for w in progress.warnings)
    # 금액 원값은 warnings에 미노출(§8-2 노출 최소)
    assert not any("200000" in w or "200,000" in w for w in progress.warnings)


@pytest.mark.anyio
async def test_한도_소진_상태면_buy_blocked_명시_복원(tmp_path):
    """매도만으로 일일 금액 상한을 넘긴 상태(live에서도 가능 — 매도는 상한
    무시 누적) → 재기동 시 래치가 즉시 복원돼 후보 선정 자체를 건너뛴다."""
    service, store, broker = _seed_service(
        tmp_path, [("sell", 600_000_000, 1)])
    await service.run()
    assert service._caps.buy_blocked is True
    assert service._entries_done is False      # 매수 발주는 없었음
    assert broker.placed == []                 # buy_blocked 게이트로 진입 없음


@pytest.mark.anyio
async def test_정확히_상한이면_래치는_아직_아님(tmp_path):
    """check()의 strict > 판정과 동일 — 상한 정확 도달은 live 연속 실행에서도
    래치 전(다음 매수 check가 래치)."""
    service, _store, _broker = _seed_service(
        tmp_path, [("sell", 100_000_000, 1)] * 5)  # 건수 5==max, 금액 500M==max
    await service.run()
    assert service._caps.order_count == 5
    assert service._caps.buy_blocked is False


@pytest.mark.anyio
async def test_건수_초과도_buy_blocked_복원(tmp_path):
    """exceeds_daily 공유 판정의 count 분기 — 구조적으로 live에서는 매수
    차단이 선행돼 도달 어려우나(트레이더 Minor), 판정 대칭성을 회귀 고정."""
    service, _store, _broker = _seed_service(
        tmp_path, [("sell", 1_000, 1)] * 6)   # 건수 6 > max 5
    await service.run()
    assert service._caps.buy_blocked is True


@pytest.mark.anyio
async def test_주문_없는_날은_시딩_무발생(tmp_path):
    service, _store, _broker = _seed_service(tmp_path, [])
    await service.run()
    assert service._caps.order_count == 0
    assert not any("daily caps seeded" in w
                   for w in service.progress().warnings)


@pytest.mark.anyio
async def test_시장가_주문_기록이_est_krw를_영속한다(tmp_path):
    """서비스 감사 콜백(_record_order_for) → store → daily_order_usage의
    est 배선 end-to-end — 시장가 OrderRequest.ref_price가 시딩 금액으로
    돌아온다(P6-T1 트레이더 Critical의 서비스측 회귀)."""
    from app.domain.broker import (OrderAck, OrderRequest, OrderSide,
                                   OrderStyle)
    service, store, _broker = _seed_service(tmp_path, [])
    service._run_id = store.create_run("{}", "mock")
    market_req = OrderRequest(symbol="005930", side=OrderSide.SELL,
                              style=OrderStyle.MARKET, quantity=3,
                              ref_price=99_000)
    service._record_order_for(None)(
        OrderAck(order_no="M1", message="ok"), market_req, "submitted")
    usage = store.daily_order_usage(DAY, "mock")
    assert usage.order_krw == 297_000
    assert usage.has_buy is False
