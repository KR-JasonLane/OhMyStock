"""TradingService(P5 Task 7) — 조립·수명주기·§8-1 캡·잔고 대사 하드 게이트.

서비스 레벨 시나리오는 사이클 수가 비결정(폴링 루프)이라 fake 브로커는
스크립트 소진 시 관대한 기본값(빈 응답/마지막 값 반복)을 쓴다 — 단위
fake(conftest.FakeOrderPortBase)의 fail-loud와 다른 계약(문서화)."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine

from app.core.background_service import StopMode
from app.domain.broker import (Balance, Deposit, MarketData, OpenOrder,
                               OrderAck, OrderSide, Position, Quote)
from app.domain.trading.config import TradingConfig
from app.domain.trading.models import ExitReason, PositionState, TradePosition
from app.domain.trading.monitor import ExitAction
from app.domain.trading.service import OrderCaps, TradingService
from app.store.models import Base
from app.store.trading_store import EntryContext, TradingStore

KST = timezone(timedelta(hours=9))
T0 = datetime(2026, 7, 22, 9, 10, tzinfo=KST)   # 화요일 09:10 — 진입 창 안

CFG = TradingConfig(max_single_order_krw=100_000_000, max_daily_orders=100,
                    max_daily_order_krw=500_000_000,
                    min_avg_trading_value_krw=0,
                    limit_order_timeout_sec=3.0, exit_limit_timeout_sec=3.0,
                    poll_interval_sec=1.0, quote_failure_threshold=2)

LATEST = {"picks": [{"symbol": "005930", "rank": 1}],
          "score_reference_date": "2026-07-21"}  # 전 거래일(월)

CTX = EntryContext(symbol="005930", name="삼성전자", market="kospi",
                   audit_info="정상", state="",
                   signal_price=100_000, avg_trading_value_krw=10**12)


def _md(symbol: str, price: int) -> MarketData:
    q = Quote(symbol=symbol, name="삼성전자", price=price,
              change_rate=Decimal("0"), volume=0)
    return MarketData(quote=q, bid=price - 100, ask=price + 100)


def _bpos(symbol="005930", qty=9, avg=100_050) -> Position:
    return Position(symbol=symbol, name="삼성전자", quantity=qty,
                    avg_price=avg, current_price=avg, eval_amount=avg * qty)


class Cal:
    KST = KST

    def __init__(self, market_hours: list[bool] | None = None):
        self._hours = list(market_hours or [])

    def is_trading_day(self, d) -> bool:
        return True

    def is_market_hours(self, now) -> bool:
        return self._hours.pop(0) if self._hours else False

    def held_business_days(self, entry_date, now) -> int:
        return 0


class FakeBroker:
    """OrderPort+BrokerPort 표면. 스크립트 소진 시 관대(빈/반복) — 서비스
    루프의 사이클 수 비결정성 흡수."""

    def __init__(self, quotes=None, open_orders=None, balances=None,
                 deposit_available=10_000_000):
        self._quotes = list(quotes or [])
        self._open_orders = list(open_orders or [])
        self._balances = list(balances or [Balance((), 0, 0)])
        self._deposit = deposit_available
        self.placed: list = []
        self.cancelled: list = []
        self._seq = 0

    async def get_quotes(self, symbols):
        book = self._quotes.pop(0) if self._quotes else (
            self._last_book if hasattr(self, "_last_book") else {})
        if isinstance(book, Exception):
            raise book
        self._last_book = book
        return [book[s] for s in symbols if s in book]

    async def place_order(self, req):
        self._seq += 1
        self.placed.append(req)
        return OrderAck(order_no=f"ORD{self._seq}", message="ok")

    async def cancel_order(self, order_no, symbol):
        self.cancelled.append(order_no)
        return OrderAck(order_no=f"CXL{order_no}", message="cancelled")

    async def get_open_orders(self):
        item = self._open_orders.pop(0) if self._open_orders else None
        if isinstance(item, Exception):
            raise item
        if item is None:
            return []
        return [OpenOrder(order_no=f"ORD{self._seq}", symbol="005930",
                          side=OrderSide.BUY, order_qty=9,
                          unfilled_qty=item, order_price=100_100,
                          status="접수")]

    async def get_balance(self):
        if len(self._balances) > 1:
            return self._balances.pop(0)
        return self._balances[0]

    async def get_deposit(self):
        return Deposit(total=self._deposit, available=self._deposit)


class StoreForTest(TradingStore):
    """entry_context/instrument_state만 준비된 값으로 대체(수집 테이블 시드
    없이 조인 재료 주입) — 나머지는 실제 sqlite 영속."""

    def __init__(self, engine, contexts=None):
        super().__init__(engine, now=lambda: T0)
        self._contexts = contexts or {}

    def entry_context(self, symbols, signal_date, avg_days=20):
        return {s: c for s, c in self._contexts.items() if s in symbols}

    def instrument_state(self, symbol):
        return None


async def _yield_sleep(_):
    await asyncio.sleep(0)


@pytest.fixture
def store(tmp_path) -> StoreForTest:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'trade.db'}")
    Base.metadata.create_all(engine)
    return StoreForTest(engine, contexts={"005930": CTX})


def _service(broker, store, calendar=None, latest=LATEST) -> TradingService:
    return TradingService(broker, broker, store, CFG,
                          calendar or Cal(), lambda: latest,
                          sleep=_yield_sleep, now=lambda: T0)


# ── OrderCaps (§8-1) ────────────────────────────────────────────────────

def test_caps_매도는_상한을_넘어도_절대_차단하지_않는다():
    caps = OrderCaps(CFG)
    caps.check(10**12, OrderSide.SELL)  # 단건 상한 초과 — 예외 없음
    for _ in range(200):                # 일일 건수 상한 초과 — 예외 없음
        caps.check(1, OrderSide.SELL)
    assert caps.order_count == 201      # 기록은 전부 남는다


def test_caps_매수_단건_상한():
    caps = OrderCaps(CFG)
    with pytest.raises(ValueError, match="single order cap"):
        caps.check(CFG.max_single_order_krw + 1, OrderSide.BUY)


def test_caps_매수_일일_상한은_래치로_신규만_정지():
    caps = OrderCaps(CFG)
    for _ in range(CFG.max_daily_orders):
        caps.check(1, OrderSide.SELL)   # 건수 소진(매도)
    with pytest.raises(ValueError, match="daily order cap"):
        caps.check(1, OrderSide.BUY)    # 매수는 차단 + 래치
    assert caps.buy_blocked is True
    caps.check(1, OrderSide.SELL)       # 매도는 래치 후에도 통과


# ── 수명주기 (§9 감사) ──────────────────────────────────────────────────

@pytest.mark.anyio
async def test_reconcile_실패도_finish_run이_기록한다(store):
    broker = FakeBroker(open_orders=[ConnectionError("broker down")])
    service = _service(broker, store)
    await service.run()   # 블라인드 기동 금지 — 실패로 표면화
    run = store.latest_run()
    assert run["status"] == "failed"
    assert "ConnectionError" in run["failure_reason"]
    assert service.is_running() is False


@pytest.mark.anyio
async def test_진입_해피패스와_잔고_대사_교정(store):
    broker = FakeBroker(
        quotes=[{"005930": _md("005930", 100_000)},   # 진입 조인
                {"005930": _md("005930", 100_500)}],  # 감시 사이클
        open_orders=[None,          # reconcile
                     None, None],   # 진입 지정가 체결 확인(부재 2회)
        balances=[Balance((_bpos(qty=9, avg=100_050),), 0, 0)])
    service = _service(broker, store, calendar=Cal([True, False]))
    await service.run()
    run = store.latest_run()
    assert run["status"] == "succeeded"
    rows, _ = store.open_positions()
    [(pid, pos)] = rows
    assert pos.state is PositionState.ENTERED
    # 잔고 대사(kt00018 ground truth) — 추정 발주가가 아니라 실측 평단
    assert pos.entry_price == 100_050 and pos.quantity == 9
    assert broker.placed[0].side is OrderSide.BUY
    orders = store.orders_for_position(pid)
    assert orders and orders[0].status == "submitted"  # 감사 이력


@pytest.mark.anyio
async def test_잔고에_없는_진입은_유령으로_즉시_해소(store):
    broker = FakeBroker(
        quotes=[{"005930": _md("005930", 100_000)}],
        open_orders=[None, None, None],
        balances=[Balance((), 0, 0)])   # 체결됐다는데 잔고 없음
    service = _service(broker, store, calendar=Cal([True, False]))
    await service.run()
    rows, _ = store.open_positions()
    assert rows == []  # CLOSED — 감시 대상 아님
    assert any("phantom" in w for w in service.progress().warnings)


@pytest.mark.anyio
async def test_낡은_신호는_진입_스킵(store):
    stale = {"picks": [{"symbol": "005930", "rank": 1}],
             "score_reference_date": "2026-07-01"}
    broker = FakeBroker(open_orders=[None])
    service = _service(broker, store, calendar=Cal([True, False]),
                       latest=stale)
    await service.run()
    assert broker.placed == []
    assert any("signal date mismatch" in w
               for w in service.progress().warnings)


@pytest.mark.anyio
async def test_미래_신호도_진입_스킵(store):
    """트레이더 R6 — look-ahead 차단: 재생 시점(과거 앵커)보다 미래의
    분석 픽(실시계 파이프라인 산출)이 통과하면 재생 세계에 존재하지 않던
    정보로 진입한다. 프로덕션에서도 미래 신호는 데이터 손상 신호."""
    future = {"picks": [{"symbol": "005930", "rank": 1}],
              "score_reference_date": "2027-01-04"}
    broker = FakeBroker(open_orders=[None])
    service = _service(broker, store, calendar=Cal([True, False]),
                       latest=future)
    await service.run()
    assert broker.placed == []
    assert any("signal date mismatch" in w
               for w in service.progress().warnings)


@pytest.mark.anyio
async def test_킬스위치_전량청산과_감사_기록(store):
    run_id = store.create_run("{}")
    pos = TradePosition(symbol="005930", name="삼성전자", market="kospi",
                        state=PositionState.ENTERED, entry_price=100_000,
                        quantity=9, peak_price=100_000, trailing_active=False,
                        entered_at=T0)
    store.create_position(run_id, pos)
    broker = FakeBroker(
        quotes=[{"005930": _md("005930", 100_000)}],
        open_orders=[None,        # reconcile
                     None, None],  # 청산 시장가 체결 확인
        balances=[Balance((_bpos(qty=9, avg=100_000),), 0, 0),
                  Balance((), 0, 0)])   # 청산 후 잔고 비움(하드 게이트 통과)
    service = _service(broker, store, calendar=Cal([True] * 50),
                       latest=None)
    task = asyncio.create_task(service.run())
    for _ in range(20):
        await asyncio.sleep(0)
    service.request_stop(StopMode.LIQUIDATE_ALL)
    await task
    run = store.latest_run()
    assert run["status"] == "stopped"
    assert run["stopped_by_kill_switch"] is True
    assert run["kill_switch_mode"] == "liquidate_all"
    sells = [r for r in broker.placed if r.side is OrderSide.SELL]
    assert sells and sells[0].quantity == 9
    rows, _ = store.open_positions()
    assert rows == []  # CLOSED


@pytest.mark.anyio
async def test_강제_취소도_감사에_성공으로_기록되지_않는다(store):
    """보안 P5-T7 #1 — 셧다운 취소가 status='succeeded'로 남으면 §9 감사가
    강제 중단을 정상 종료로 오판한다."""
    broker = FakeBroker(open_orders=[None])
    service = _service(broker, store, calendar=Cal([True] * 1000), latest=None)
    task = asyncio.create_task(service.run())
    for _ in range(300):  # create_run(스레드) 완료 후 루프 진입까지 대기
        await asyncio.sleep(0.005)
        if service._run_id is not None:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    run = store.latest_run()
    assert run["status"] == "stopped"
    assert "cancelled" in run["failure_reason"]
    assert service.is_running() is False


@pytest.mark.anyio
async def test_reconcile_종결은_보유_집합에서_즉시_제거된다(store):
    """개발자 P5-T7 Critical #1 — reconcile CLOSE(⑥-b 외부 처분)가 _pos_ids에
    남으면 그 심볼 재진입이 막히고 슬롯·positions_count가 오염된다."""
    run_id = store.create_run("{}")
    pos = TradePosition(symbol="005930", name="삼성전자", market="kospi",
                        state=PositionState.ENTERED, entry_price=100_000,
                        quantity=9, peak_price=100_000, trailing_active=False,
                        entered_at=T0)
    store.create_position(run_id, pos)
    broker = FakeBroker(open_orders=[None],
                        balances=[Balance((), 0, 0)])  # 브로커 무보유 → ⑥-b
    service = _service(broker, store, calendar=Cal([]), latest=None)
    await service.run()
    rows, _ = store.open_positions()
    assert rows == []  # CLOSED 확정
    assert service.progress().positions_count == 0  # stale 매핑 없음


@pytest.mark.anyio
async def test_킬스위치는_미체결_청산을_종결까지_추적한다(store):
    """트레이더 P5-T7 C1 — 발행만 하고 반환하면 미체결 매도가 무감시 방치.
    pending 추적을 CLOSED/EXIT_FAILED 종결까지 유지한 뒤에만 run이 끝난다."""
    run_id = store.create_run("{}")
    pos = TradePosition(symbol="005930", name="삼성전자", market="kospi",
                        state=PositionState.ENTERED, entry_price=100_000,
                        quantity=9, peak_price=100_000, trailing_active=False,
                        entered_at=T0)
    store.create_position(run_id, pos)
    broker = FakeBroker(
        quotes=[{"005930": _md("005930", 100_000)}],
        open_orders=[None,          # reconcile
                     10, 10, 10,    # 청산 시장가 미체결 → pending 추적
                     None],         # 다음 사이클 재확인 — 소멸=체결
        balances=[Balance((_bpos(qty=9, avg=100_000),), 0, 0),
                  Balance((), 0, 0)])
    service = _service(broker, store, calendar=Cal([True] * 50), latest=None)
    task = asyncio.create_task(service.run())
    for _ in range(20):
        await asyncio.sleep(0)
    service.request_stop(StopMode.LIQUIDATE_ALL)
    await task
    assert broker.cancelled == []  # 매도 주문 취소 금지(6b 계약)
    sells = [r for r in broker.placed if r.side is OrderSide.SELL]
    assert len(sells) == 1  # pending 가드 — 중복 재발주 없음
    rows, _ = store.open_positions()
    assert rows == []  # CLOSED 종결 후에만 반환
    assert store.latest_run()["status"] == "stopped"


@pytest.mark.anyio
async def test_유령_판정은_유예_재조회_후에만(store):
    """트레이더 P5-T7 C2 — 잔고 전파 지연 단발 스냅샷으로 실체결 포지션을
    비가역 CLOSED하면 영구 무감시. 재조회에서 발견되면 정상 교정."""
    broker = FakeBroker(
        quotes=[{"005930": _md("005930", 100_000)}],
        open_orders=[None, None, None],
        balances=[Balance((), 0, 0),                       # 1차 결측(지연)
                  Balance((_bpos(qty=9, avg=100_050),), 0, 0)])  # 재조회 발견
    service = _service(broker, store, calendar=Cal([True, False]))
    await service.run()
    rows, _ = store.open_positions()
    [(_pid, pos)] = rows
    assert pos.state is PositionState.ENTERED  # 유령 오판 없음
    assert pos.entry_price == 100_050


@pytest.mark.anyio
async def test_하드게이트_재오픈은_원본_market을_보존한다(store):
    """트레이더 P5-T7 C3 — open_positions는 CLOSED를 제외하므로 방금 닫힌
    행의 market을 kospi로 폴백하면 코스닥/ETF 틱·세율이 오적용된다.
    실제 순서(모니터가 CLOSED persist → _post_actions) 그대로 재현."""
    run_id = store.create_run("{}")
    pos = TradePosition(symbol="247540", name="에코프로비엠", market="kosdaq",
                        state=PositionState.ENTERED, entry_price=100_000,
                        quantity=9, peak_price=100_000, trailing_active=False,
                        entered_at=T0)
    pid = store.create_position(run_id, pos)
    # 모니터가 이미 CLOSED로 영속한 상태를 재현
    store.save_position_snapshot(pid, TradePosition(
        symbol="247540", name="에코프로비엠", market="kosdaq",
        state=PositionState.CLOSED, entry_price=100_000, quantity=9,
        peak_price=100_000, trailing_active=False, entered_at=T0,
        exit_reason=ExitReason.STOP_LOSS, closed_at=T0))
    broker = FakeBroker(balances=[Balance(
        (Position(symbol="247540", name="에코프로비엠", quantity=9,
                  avg_price=100_000, current_price=100_000,
                  eval_amount=900_000),), 0, 0)])
    service = _service(broker, store)
    service._on_accepted()
    service._run_id = run_id
    service._pos_ids = {"247540": pid}
    action = ExitAction("247540", ExitReason.STOP_LOSS, PositionState.CLOSED,
                        9, exit_price=None, requires_reconcile=True)
    await service._post_actions([action])
    reopened = store.get_position(pid)
    assert reopened.state is PositionState.ENTERED
    assert reopened.market == "kosdaq"  # kospi 폴백 오분류 금지


@pytest.mark.anyio
async def test_단건_상한_위반은_해당_후보만_스킵하고_배치_계속(store):
    """트레이더 P5-T7 I5 — 후보 단위 위반(SingleOrderCapExceeded)이 전역
    소진(DailyCapExceeded)처럼 배치를 통째로 중단하면 정상 후보의 하루치
    진입 기회를 잃는다."""
    cfg = TradingConfig(max_single_order_krw=800_000, max_daily_orders=100,
                        max_daily_order_krw=500_000_000,
                        min_avg_trading_value_krw=0,
                        limit_order_timeout_sec=3.0, poll_interval_sec=1.0)
    ctx2 = EntryContext(symbol="000660", name="SK하이닉스", market="kospi",
                        audit_info="정상", state="",
                        signal_price=530_000, avg_trading_value_krw=10**12)
    store._contexts["000660"] = ctx2
    latest = {"picks": [{"symbol": "005930", "rank": 1},
                        {"symbol": "000660", "rank": 2}],
              "score_reference_date": "2026-07-21"}
    # 슬롯 ≈1M: 005930(십만원×9주=90만) > 80만 캡 위반, 000660(53만×1주) 통과
    broker = FakeBroker(
        quotes=[{"005930": _md("005930", 100_000),
                 "000660": _md("000660", 530_000)},   # 배치 스냅샷
                {"005930": _md("005930", 100_000)},   # 후보1 재조회
                {"000660": _md("000660", 530_000)}],  # 후보2 재조회
        open_orders=[None, None, None],
        balances=[Balance((_bpos("000660", 1, 530_000),), 0, 0)])
    service = TradingService(broker, broker, store, cfg, Cal([True, False]),
                             lambda: latest, sleep=_yield_sleep,
                             now=lambda: T0)
    await service.run()
    assert [r.symbol for r in broker.placed] == ["000660"]  # 배치 계속
    assert any("single-order cap" in w for w in service.progress().warnings)
    rows, _ = store.open_positions()
    assert [pos.symbol for _pid, pos in rows] == ["000660"]


@pytest.mark.anyio
async def test_재진입_쿨다운은_최근_청산_심볼을_제외한다(store):
    """트레이더 P5-T7 I6 — reentry_cooldown_min 실배선(§8-1). DB closed_at
    기반이라 재기동에도 유지."""
    run_id = store.create_run("{}")
    pid = store.create_position(run_id, TradePosition(
        symbol="005930", name="삼성전자", market="kospi",
        state=PositionState.PENDING_ENTRY, entry_price=100_000, quantity=9,
        peak_price=100_000, trailing_active=False))
    store.save_position_snapshot(pid, TradePosition(
        symbol="005930", name="삼성전자", market="kospi",
        state=PositionState.CLOSED, entry_price=100_000, quantity=9,
        peak_price=100_000, trailing_active=False, entered_at=T0,
        exit_reason=ExitReason.STOP_LOSS,
        closed_at=T0 - timedelta(minutes=10)))  # 10분 전 청산(쿨다운 30분)
    broker = FakeBroker(quotes=[{"005930": _md("005930", 100_000)}],
                        open_orders=[None])
    service = _service(broker, store, calendar=Cal([True, False]))
    await service.run()
    assert broker.placed == []
    assert any("cooldown" in w for w in service.progress().warnings)


@pytest.mark.anyio
async def test_순수_진입_실패는_ENTRY_FAILED로_확정(store):
    # 시장가조차 0체결(requires_reconcile 아님) → ENTRY_FAILED + 매핑 제거
    broker = FakeBroker(
        quotes=[{"005930": _md("005930", 100_000)}],
        open_orders=[None] + [10] * 3 + [10] * 3,
        balances=[Balance((), 0, 0)])
    service = _service(broker, store, calendar=Cal([True, False]))
    await service.run()
    rows, _ = store.open_positions()
    assert rows == []
    assert service.progress().positions_count == 0
    assert any("entry failed" in w for w in service.progress().warnings)


@pytest.mark.anyio
async def test_requires_reconcile_CLOSED는_잔고_잔존시_재오픈(store):
    """하드 게이트(P5-T6c 보안 #2) — CLOSED 확정 전 잔고 교차 검증."""
    run_id = store.create_run("{}")
    pos = TradePosition(symbol="005930", name="삼성전자", market="kospi",
                        state=PositionState.ENTERED, entry_price=100_000,
                        quantity=9, peak_price=100_000, trailing_active=False,
                        entered_at=T0)
    pid = store.create_position(run_id, pos)
    broker = FakeBroker(balances=[Balance((_bpos(qty=9, avg=100_000),), 0, 0)])
    service = _service(broker, store)
    service._on_accepted()
    service._run_id = run_id
    service._pos_ids = {"005930": pid}
    action = ExitAction("005930", ExitReason.STOP_LOSS, PositionState.CLOSED,
                        9, exit_price=None, requires_reconcile=True)
    await service._post_actions([action])
    rows, _ = store.open_positions()
    [(rid, reopened)] = rows
    assert rid == pid and reopened.state is PositionState.ENTERED
    assert reopened.quantity == 9
    assert any("overturned" in w for w in service.progress().warnings)
