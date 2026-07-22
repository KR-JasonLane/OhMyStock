"""PositionMonitor(6b) — 감시 사이클 전 경로를 fake OrderPort/캘린더/스토어로
검증. sleep 주입으로 결정적.

폴링 계약(execution.poll_unfilled — 6a C1과 동일): exit_limit_timeout_sec=3.0/
interval=1.0 기준 유예 sleep 1회 + 폴 3회, 미관측 주문의 부재는 연속 2회 확인."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.broker import MarketData, OrderSide, OrderStyle, Quote
from tests.trading.conftest import FakeOrderPortBase
from app.domain.trading import costs
from app.domain.trading.config import TradingConfig
from app.domain.trading.models import (ExitPhase, ExitReason, PositionState,
                                       TradePosition)
from app.domain.trading.monitor import PositionMonitor

KST = timezone(timedelta(hours=9))
# 화요일 10:00 KST 장중
T0 = datetime(2026, 7, 22, 10, 0, tzinfo=KST)

CFG = TradingConfig(max_single_order_krw=100_000_000, max_daily_orders=100,
                    max_daily_order_krw=500_000_000,
                    min_avg_trading_value_krw=0,
                    exit_limit_timeout_sec=3.0, poll_interval_sec=1.0,
                    quote_failure_threshold=2)


class FakeCalendar:
    KST = KST

    def __init__(self, held=0, market_open=True):
        self.held = held
        self.market_open = market_open

    def held_business_days(self, entry_date: date, now: datetime) -> int:
        return self.held

    def is_market_hours(self, now: datetime) -> bool:
        return self.market_open


def _md(symbol: str, price: int, bid: int | None = None) -> MarketData:
    q = Quote(symbol=symbol, name="테스트", price=price,
              change_rate=Decimal("0"), volume=0)
    return MarketData(quote=q, bid=bid if bid is not None else price - 100,
                      ask=price + 100)


class FakeOrders(FakeOrderPortBase):
    """공용 베이스(conftest — 개발자 P5-T6b #4) + 6b 전용 get_quotes.
    quotes_script: get_quotes 호출별 시나리오 — dict{symbol: MarketData}
    또는 Exception(소진 시 AssertionError)."""

    def __init__(self, quotes_script=None, open_orders_script=None,
                 cancel_script=None, place_script=None):
        super().__init__(open_orders_script, cancel_script, place_script)
        self._quotes = list(quotes_script or [])

    async def get_quotes(self, symbols):
        self.calls.append(("quotes", tuple(symbols)))
        assert self._quotes, "quotes_script exhausted"
        item = self._quotes.pop(0)
        if isinstance(item, Exception):
            raise item
        return [item[s] for s in symbols if s in item]


class FakeStore:
    """persist_position 콜백 뒷단 — symbol 키 dict. trailing_active/peak가
    DB 영속값 그대로 다음 사이클 입력이 되는 왕복(§6-2)을 재현한다."""

    def __init__(self, *positions: TradePosition):
        self.rows: dict[str, TradePosition] = {p.symbol: p for p in positions}
        self.history: list[TradePosition] = []

    def persist(self, pos: TradePosition) -> None:
        self.rows[pos.symbol] = pos
        self.history.append(pos)

    def open_positions(self) -> list[TradePosition]:
        return [p for p in self.rows.values()
                if p.state is PositionState.ENTERED]


async def _no_sleep(_):
    return None


def _pos(symbol="005930", entry=100_000, peak=None, trailing=False,
         qty=10, state=PositionState.ENTERED) -> TradePosition:
    return TradePosition(symbol=symbol, name="테스트", market="kospi",
                         state=state, entry_price=entry,
                         quantity=qty, peak_price=peak or entry,
                         trailing_active=trailing, entered_at=T0)


def _monitor(fake, store, calendar=None, caps=None, on_order=None,
             lookup=None, sleep=None) -> PositionMonitor:
    return PositionMonitor(fake, CFG, calendar or FakeCalendar(),
                           caps or (lambda *_: None), store.persist,
                           on_order=on_order, lookup_instrument_state=lookup,
                           sleep=sleep or _no_sleep, now=lambda: T0)


@pytest.mark.anyio
async def test_청산_조건_미충족은_관측만_영속하고_주문_없음():
    store = FakeStore(_pos())
    fake = FakeOrders(quotes_script=[{"005930": _md("005930", 101_000)}])
    actions = await _monitor(fake, store).poll_once(store.open_positions(), T0)
    assert actions == [] and fake.placed == []
    assert store.rows["005930"].peak_price == 101_000  # 고점 갱신 영속(§6-2)
    assert store.rows["005930"].trailing_active is False


@pytest.mark.anyio
async def test_손절은_즉시_시장가_그리고_실현손익_기록():
    store = FakeStore(_pos())
    fake = FakeOrders(quotes_script=[{"005930": _md("005930", 94_000,
                                                    bid=93_900)}],
                      open_orders_script=[None, None])
    actions = await _monitor(fake, store).poll_once(store.open_positions(), T0)
    assert len(actions) == 1
    act = actions[0]
    assert act.reason is ExitReason.STOP_LOSS
    assert act.state is PositionState.CLOSED
    assert act.exit_price == 93_900  # 추정 = 최우선 매수호가
    assert act.realized_pnl == costs.realized_pnl(
        "kospi", 100_000 * 10, 93_900 * 10, CFG)
    assert [r.style for r in fake.placed] == [OrderStyle.MARKET]
    assert fake.placed[0].side is OrderSide.SELL
    assert store.rows["005930"].state is PositionState.CLOSED
    assert store.rows["005930"].realized_pnl == act.realized_pnl


@pytest.mark.anyio
async def test_trailing_active_DB_왕복_계약():
    """필수 캐리(P5-T3 트레이더): 사이클 간 trailing_active/peak는 fake store
    왕복 값 그대로 — monitor가 재계산해 넘기면 이 테스트가 깨져야 한다."""
    store = FakeStore(_pos())
    # 사이클 1: +6% — 래치 온+고점 갱신, 청산 없음
    fake1 = FakeOrders(quotes_script=[{"005930": _md("005930", 106_000)}])
    acts1 = await _monitor(fake1, store).poll_once(store.open_positions(), T0)
    assert acts1 == [] and fake1.placed == []
    assert store.rows["005930"].trailing_active is True   # 래치 영속
    assert store.rows["005930"].peak_price == 106_000
    # 사이클 2: 영속값을 그대로 입력 — 고점 대비 하락 → 트레일링 스톱
    # (+6% 고점의 보간 폭 ≈4.33% → 스톱 ≈101,407; 101,000은 그 아래)
    fake2 = FakeOrders(quotes_script=[{"005930": _md("005930", 101_000,
                                                     bid=101_000)}],
                       open_orders_script=[None, None])
    acts2 = await _monitor(fake2, store).poll_once(store.open_positions(), T0)
    assert len(acts2) == 1 and acts2[0].reason is ExitReason.TRAILING_STOP
    assert store.rows["005930"].state is PositionState.CLOSED


@pytest.mark.anyio
async def test_익절_백스톱은_지정가로_나가고_체결시_CLOSED():
    store = FakeStore(_pos())
    fake = FakeOrders(quotes_script=[{"005930": _md("005930", 111_000)}],
                      open_orders_script=[None, None])
    actions = await _monitor(fake, store).poll_once(store.open_positions(), T0)
    assert actions[0].reason is ExitReason.TAKE_PROFIT
    assert actions[0].state is PositionState.CLOSED
    assert fake.placed[0].style is OrderStyle.LIMIT
    assert fake.placed[0].limit_price == 111_000  # 현재가, 틱 정렬 유지
    # exit_phase 경유 영속: EXITING(LIMIT_SUBMITTED) → CLOSED
    phases = [p.exit_phase for p in store.history
              if p.state is PositionState.EXITING]
    assert ExitPhase.LIMIT_SUBMITTED in phases


@pytest.mark.anyio
async def test_익절_지정가_미체결은_취소_후_시장가_폴백():
    store = FakeStore(_pos())
    fake = FakeOrders(quotes_script=[{"005930": _md("005930", 111_000)}],
                      open_orders_script=[10, 10, 10, 10, None])
    actions = await _monitor(fake, store).poll_once(store.open_positions(), T0)
    assert actions[0].state is PositionState.CLOSED
    # 0체결 → 시장가 전량 — 추정가는 스테일 지정가가 아니라 관측 bid(개발자 #2)
    assert actions[0].exit_price == 110_900
    assert actions[0].requires_reconcile is False
    assert fake.cancelled == ["ORD1"]
    assert [r.style for r in fake.placed] == [OrderStyle.LIMIT,
                                              OrderStyle.MARKET]
    # 취소 직전 CANCEL_REQUESTED fail-closed 영속(아키텍트 #3 — entry와 대칭)
    phases = [p.exit_phase for p in store.history
              if p.state is PositionState.EXITING]
    assert phases == [ExitPhase.LIMIT_SUBMITTED, ExitPhase.CANCEL_REQUESTED,
                      ExitPhase.MARKET_SUBMITTED]


@pytest.mark.anyio
async def test_익절_취소_실패는_시장가_강행_없이_추적으로():
    """이중 매도 가드(6a 이중 매수 가드와 동일 원리) — 취소 실패 = 주문 상태
    불명(직전 체결 가능). 시장가 재발주 대신 _pending 추적."""
    store = FakeStore(_pos())
    fake = FakeOrders(quotes_script=[{"005930": _md("005930", 111_000)}],
                      open_orders_script=[10, 10, 10],
                      cancel_script=[RuntimeError("cancel rejected")])
    mon = _monitor(fake, store)
    actions = await mon.poll_once(store.open_positions(), T0)
    assert actions[0].state is PositionState.EXITING
    assert actions[0].requires_reconcile is True
    assert [r.style for r in fake.placed] == [OrderStyle.LIMIT]  # 강행 없음
    assert mon.warnings  # pending 경고 노출


@pytest.mark.anyio
async def test_시장가_미체결은_취소하지_않고_추적_후_다음_사이클_확정():
    """VI/동시호가 계약(§6-4) — 매도 주문을 성급히 취소하면 청산 무산.
    추적 유지 → 다음 사이클에서 주문 소멸 관측 → CLOSED."""
    store = FakeStore(_pos())
    fake = FakeOrders(
        quotes_script=[{"005930": _md("005930", 94_000, bid=93_900)}],
        open_orders_script=[10, 10, 10,  # 사이클1: 체결 확인 타임아웃
                            None])       # 사이클2: _check_pending — 소멸=체결
    mon = _monitor(fake, store)
    acts1 = await mon.poll_once(store.open_positions(), T0)
    assert acts1[0].state is PositionState.EXITING
    assert fake.cancelled == []  # 취소 금지
    acts2 = await mon.poll_once([], T0)
    assert acts2[0].state is PositionState.CLOSED
    assert acts2[0].reason is ExitReason.STOP_LOSS
    # 지연 확정 — est_price 스테일 + 미추적 창 체결 → 즉시 대사 표식(트레이더 I6)
    assert acts2[0].requires_reconcile is True
    assert store.rows["005930"].state is PositionState.CLOSED


@pytest.mark.anyio
async def test_pending_확인_중_BrokerError는_흡수하고_추적_유지():
    store = FakeStore(_pos())
    fake = FakeOrders(
        quotes_script=[{"005930": _md("005930", 94_000)}],
        open_orders_script=[10, 10, 10, ConnectionError("down"), None])
    mon = _monitor(fake, store)
    await mon.poll_once(store.open_positions(), T0)
    acts2 = await mon.poll_once([], T0)   # 조회 실패 — 전면 중단 금지
    assert acts2 == []
    acts3 = await mon.poll_once([], T0)   # 복구 — 소멸 관측 → CLOSED
    assert acts3[0].state is PositionState.CLOSED


@pytest.mark.anyio
async def test_장마감까지_생존한_청산_주문은_EXIT_FAILED():
    store = FakeStore(_pos())
    cal = FakeCalendar()
    fake = FakeOrders(
        quotes_script=[{"005930": _md("005930", 94_000)}],
        open_orders_script=[10, 10, 10, 10])  # 사이클2에도 주문 생존
    mon = _monitor(fake, store, calendar=cal)
    await mon.poll_once(store.open_positions(), T0)
    cal.market_open = False  # 장 마감
    acts2 = await mon.poll_once([], T0)
    assert acts2[0].state is PositionState.EXIT_FAILED
    assert acts2[0].requires_reconcile is True
    assert store.rows["005930"].state is PositionState.EXIT_FAILED


@pytest.mark.anyio
async def test_전체_시세_조회_실패는_판정_스킵_임계초과시_경고():
    store = FakeStore(_pos())
    err = ConnectionError("down")
    fake = FakeOrders(quotes_script=[err, err, err])
    mon = _monitor(fake, store)
    for _ in range(3):  # threshold=2 — 3번째에 경고 전환
        acts = await mon.poll_once(store.open_positions(), T0)
        assert acts == [] and fake.placed == []
    assert any("unmonitored" in w for w in mon.warnings)
    # 조회 실패 ≠ 가격 불변 — 포지션 상태는 그대로(§6-4)
    assert store.rows["005930"].state is PositionState.ENTERED


@pytest.mark.anyio
async def test_특정_종목_결측은_거래정지와_네트워크를_구분():
    store = FakeStore(_pos())
    looked_up = []

    async def lookup(symbol):
        looked_up.append(symbol)
        return "거래정지"

    fake = FakeOrders(quotes_script=[{}, {}, {}])  # 조회는 성공, 종목만 결측
    mon = _monitor(fake, store, lookup=lookup)
    for _ in range(3):
        await mon.poll_once(store.open_positions(), T0)
    assert looked_up == ["005930"]  # 임계 초과 시에만 조회
    assert any("halted" in w for w in mon.warnings)


@pytest.mark.anyio
async def test_청산_발주_연속_실패는_상한에서_EXIT_FAILED_고정():
    store = FakeStore(_pos())
    quotes = [{"005930": _md("005930", 94_000)}] * 3
    fake = FakeOrders(quotes_script=quotes,
                      place_script=[RuntimeError("broker down")] * 3)
    mon = _monitor(fake, store)
    a1 = await mon.poll_once(store.open_positions(), T0)
    assert a1[0].state is PositionState.ENTERED  # 재시도 예정
    assert store.rows["005930"].state is PositionState.ENTERED  # EXITING 복원
    a2 = await mon.poll_once(store.open_positions(), T0)
    assert a2[0].state is PositionState.ENTERED
    a3 = await mon.poll_once(store.open_positions(), T0)
    assert a3[0].state is PositionState.EXIT_FAILED  # 상한(3) 도달 — 고정
    assert a3[0].requires_reconcile is True
    assert store.rows["005930"].state is PositionState.EXIT_FAILED


@pytest.mark.anyio
async def test_집행_우선순위는_손절이_익절보다_먼저():
    a = _pos(symbol="005930", entry=100_000)                   # 손절 대상
    b = _pos(symbol="000660", entry=100_000)                   # 익절 대상
    store = FakeStore(a, b)
    fake = FakeOrders(
        quotes_script=[{"005930": _md("005930", 94_000),
                        "000660": _md("000660", 111_000)}],
        open_orders_script=[None, None, None, None])
    actions = await _monitor(fake, store).poll_once([a, b], T0)
    assert [x.reason for x in actions] == [ExitReason.STOP_LOSS,
                                           ExitReason.TAKE_PROFIT]
    assert [r.style for r in fake.placed] == [OrderStyle.MARKET,
                                              OrderStyle.LIMIT]


@pytest.mark.anyio
async def test_보유기간_초과는_시장가_강제_청산():
    store = FakeStore(_pos())
    fake = FakeOrders(quotes_script=[{"005930": _md("005930", 100_000)}],
                      open_orders_script=[None, None])
    mon = _monitor(fake, store, calendar=FakeCalendar(held=9))  # 10번째 세션
    actions = await mon.poll_once(store.open_positions(), T0)
    assert actions[0].reason is ExitReason.MAX_HOLDING
    assert fake.placed[0].style is OrderStyle.MARKET


@pytest.mark.anyio
async def test_킬스위치_청산은_시세_실패에도_진행():
    store = FakeStore(_pos())
    fake = FakeOrders(quotes_script=[ConnectionError("down")],
                      open_orders_script=[None, None])
    mon = _monitor(fake, store)
    act = await mon.liquidate(store.rows["005930"], T0)
    assert act.reason is ExitReason.KILL_SWITCH
    assert act.state is PositionState.CLOSED
    assert fake.placed[0].style is OrderStyle.MARKET


@pytest.mark.anyio
async def test_entered_at_없는_ENTERED는_판정_스킵하고_경고():
    from dataclasses import replace
    broken = replace(_pos(), entered_at=None)
    store = FakeStore(broken)
    fake = FakeOrders(quotes_script=[{"005930": _md("005930", 94_000)}])
    mon = _monitor(fake, store)
    actions = await mon.poll_once([broken], T0)
    assert actions == [] and fake.placed == []  # 오염 행 — 주문 내지 않음
    assert any("entered_at" in w for w in mon.warnings)


@pytest.mark.anyio
async def test_익절_부분체결_후_시장가_폴백은_블렌디드_추정과_대사_표식():
    """트레이더 I5/개발자 #2 — 지정가 7주 + 시장가 3주 혼합 체결: 시장가분은
    관측 bid로 블렌딩, 부분체결 혼합은 잔고 대사 필수 표식."""
    store = FakeStore(_pos())
    fake = FakeOrders(quotes_script=[{"005930": _md("005930", 111_000)}],
                      open_orders_script=[3, 3, 3, None, None])
    actions = await _monitor(fake, store).poll_once(store.open_positions(), T0)
    act = actions[0]
    assert act.state is PositionState.CLOSED
    assert fake.placed[1].quantity == 3  # 시장가는 잔량만
    # (7×111,000 + 3×110,900) / 10 = 110,970
    assert act.exit_price == 110_970
    assert act.requires_reconcile is True
    assert fake.cancelled == ["ORD1"]


@pytest.mark.anyio
async def test_익절_부분체결_후_폴백_발주_실패는_잔량으로_ENTERED_복원():
    """트레이더 C1 — 체결분(7주)은 이미 팔렸다. 원 수량(10주) 복원은 다음
    사이클 초과 매도를 유발 → 잔량(3주)으로 ENTERED 복원 + 즉시 대사 표식."""
    store = FakeStore(_pos())
    fake = FakeOrders(quotes_script=[{"005930": _md("005930", 111_000)}],
                      open_orders_script=[3, 3, 3],
                      place_script=[None, RuntimeError("broker down")])
    actions = await _monitor(fake, store).poll_once(store.open_positions(), T0)
    act = actions[0]
    assert act.state is PositionState.ENTERED and act.quantity == 3
    assert act.requires_reconcile is True
    assert store.rows["005930"].state is PositionState.ENTERED
    assert store.rows["005930"].quantity == 3  # 실보유 잔량으로 복원
    assert fake.cancelled == ["ORD1"]


@pytest.mark.anyio
async def test_익절_시장가_폴백_미체결은_MARKET_SUBMITTED로_추적():
    """아키텍트 #1 재현 시나리오 — EXITING+MARKET_SUBMITTED 상태로 pending
    추적(재기동 시 reconcile ⑤ 일반화 분기의 입력)."""
    store = FakeStore(_pos())
    fake = FakeOrders(quotes_script=[{"005930": _md("005930", 111_000)}],
                      open_orders_script=[10, 10, 10, 10, 10, 10])
    mon = _monitor(fake, store)
    actions = await mon.poll_once(store.open_positions(), T0)
    assert actions[0].state is PositionState.EXITING
    assert "005930" in mon._pending
    assert store.rows["005930"].exit_phase is ExitPhase.MARKET_SUBMITTED
    assert fake.cancelled == ["ORD1"]  # 지정가 취소만 — 시장가는 취소 금지


@pytest.mark.anyio
async def test_청산_확정시_심볼_카운터와_경고가_정리된다():
    """개발자 #3 — 장수명 인스턴스에서 동일 심볼 재진입 포지션이 과거 실패
    카운트를 물려받는 누수 방지."""
    store = FakeStore(_pos())
    fake = FakeOrders(quotes_script=[{"005930": _md("005930", 94_000)}] * 2,
                      open_orders_script=[None, None],
                      place_script=[RuntimeError("hiccup")])
    mon = _monitor(fake, store)
    a1 = await mon.poll_once(store.open_positions(), T0)  # 발주 실패 1회
    assert a1[0].state is PositionState.ENTERED and mon.warnings
    a2 = await mon.poll_once(store.open_positions(), T0)  # 성공 → CLOSED
    assert a2[0].state is PositionState.CLOSED
    assert mon.warnings == []           # 경고 정리
    assert mon._submit_failures == {}   # 카운터 정리


@pytest.mark.anyio
async def test_다중_청산은_후순위_발주가_선순위_체결_대기에_막히지_않는다():
    """트레이더 C3 — 동시호가 구간에서 첫 종목의 체결 대기(최대 ~10분)가
    두 번째 종목의 주문 제출을 막으면 15:30 매칭을 놓친다. 발주는 폴 대기와
    무관하게 먼저 나가야 한다(yield하는 sleep으로 인터리빙 재현)."""
    import asyncio as _asyncio

    async def yield_sleep(_):
        await _asyncio.sleep(0)

    a = _pos(symbol="005930")
    b = _pos(symbol="000660")
    store = FakeStore(a, b)
    fake = FakeOrders(
        quotes_script=[{"005930": _md("005930", 94_000),
                        "000660": _md("000660", 93_000)}],
        open_orders_script=[None] * 8)
    mon = _monitor(fake, store, sleep=yield_sleep)
    await mon.poll_once([a, b], T0)
    order_calls = [c for c in fake.calls if c[0] in ("place", "open_orders")]
    # 두 발주가 모두 어느 폴보다 먼저 — 제출이 체결 대기에 직렬화되지 않음
    assert [c[0] for c in order_calls[:2]] == ["place", "place"]


@pytest.mark.anyio
async def test_킬스위치는_pending_추적_중_심볼에_중복_매도_안냄():
    """보안 P5-T6b #1 — poll_once의 active 필터와 대칭인 가드가 킬스위치
    경로에도 있어야 한다(추적 중 두 번째 시장가 매도 = 초과 매도 시도)."""
    store = FakeStore(_pos())
    fake = FakeOrders(quotes_script=[{"005930": _md("005930", 94_000)}],
                      open_orders_script=[10, 10, 10])
    mon = _monitor(fake, store)
    await mon.poll_once(store.open_positions(), T0)  # 미체결 → _pending
    placed_before = len(fake.placed)
    act = await mon.liquidate(store.rows["005930"], T0)
    assert len(fake.placed) == placed_before  # 두 번째 매도 없음
    assert act.state is PositionState.EXITING and "pending" in act.detail


@pytest.mark.anyio
async def test_킬스위치는_ENTERED_아닌_포지션을_스킵():
    closed = _pos(state=PositionState.CLOSED)
    from dataclasses import replace as dc_replace
    closed = dc_replace(closed, exit_reason=ExitReason.STOP_LOSS,
                        closed_at=T0)
    store = FakeStore(closed)
    fake = FakeOrders()
    act = await _monitor(fake, store).liquidate(closed, T0)
    assert fake.placed == [] and "skipped" in act.detail


@pytest.mark.anyio
async def test_caps_거부는_주문을_막고_예외_원문은_경고에_없다():
    """§8-1 캡 거부 전용 회귀(보안 Minor #3) + 경고 문자열 위생(#3):
    상태 API 노출 warnings에는 예외 타입명만, 원문(자격증명 위험) 금지."""
    sides = []

    def caps(amount, side):
        sides.append(side)
        raise ValueError("secret-dsn://user:pw@host must not leak")

    store = FakeStore(_pos())
    fake = FakeOrders(quotes_script=[{"005930": _md("005930", 94_000)}])
    mon = _monitor(fake, store, caps=caps)
    acts = await mon.poll_once(store.open_positions(), T0)
    assert fake.placed == []  # 발주 차단
    assert sides == [OrderSide.SELL]  # 방향 전달 — Task 7 매도 비차단 계약(C2)
    assert acts[0].state is PositionState.ENTERED  # 재시도 경로
    warning = next(w for w in mon.warnings if w.startswith("005930"))
    assert "ValueError" in warning and "secret-dsn" not in warning


@pytest.mark.anyio
async def test_pending_확인_연속_실패는_경고로_승격():
    """보안 P5-T6b #2 — 매도 주문이 나간 상태의 무소식은 로그만으로 침묵
    금지. 임계(threshold=2) 초과 시 상태 API 경고."""
    err = ConnectionError("down")
    store = FakeStore(_pos())
    fake = FakeOrders(quotes_script=[{"005930": _md("005930", 94_000)}],
                      open_orders_script=[10, 10, 10] + [err] * 3 + [None])
    mon = _monitor(fake, store)
    await mon.poll_once(store.open_positions(), T0)  # pending 진입
    for _ in range(3):
        await mon.poll_once([], T0)
    assert any("unverifiable" in w for w in mon.warnings)
    acts = await mon.poll_once([], T0)  # 복구 — 소멸=체결
    assert acts[0].state is PositionState.CLOSED
    assert not any("unverifiable" in w for w in mon.warnings)


def test_동시호가_및_pending_구간은_백오프_권장():
    store = FakeStore(_pos())
    mon = _monitor(FakeOrders(), store)
    assert mon.recommended_delay(T0) == CFG.poll_interval_sec
    auction = datetime(2026, 7, 22, 15, 25, tzinfo=KST)
    assert mon.recommended_delay(auction) == CFG.poll_interval_sec * 5
