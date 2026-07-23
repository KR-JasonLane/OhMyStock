"""P6 Task 2 — 진입 래치(_entries_done) 정정(스펙 §4-c, P5 정정).

종전에는 _enter_positions 호출 **전** 무조건 래치라, 09:05 첫 사이클에
분석이 없으면 09:10 분석 완료에도 그날 진입이 영구 스킵됐다(조용한 기회
상실). 이 회귀들은 판정 성립 여부 반환 계약을 고정한다:
분석 부재/불일치 → 재시도, 픽 0 → 래치, 전 후보 기술 드롭 → 재시도,
전략 탈락 → 래치."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine

from app.domain.broker import (Balance, Deposit, MarketData, OpenOrder,
                               OrderAck, OrderSide, Position, Quote)
from app.domain.trading.config import TradingConfig
from app.domain.trading.selection import (DropKind, EntryCandidate,
                                          select_entries)
from app.domain.trading.service import TradingService
from app.store.models import Base
from app.store.trading_store import EntryContext, TradingStore

KST = timezone(timedelta(hours=9))
T0 = datetime(2026, 7, 22, 9, 10, tzinfo=KST)   # 화 09:10 — 진입 창 안

CFG = TradingConfig(max_single_order_krw=100_000_000, max_daily_orders=100,
                    max_daily_order_krw=500_000_000,
                    min_avg_trading_value_krw=0,
                    limit_order_timeout_sec=3.0, exit_limit_timeout_sec=3.0,
                    poll_interval_sec=1.0, quote_failure_threshold=2)

FRESH = {"picks": [{"symbol": "005930", "rank": 1}],
         "score_reference_date": "2026-07-21"}   # 직전 거래일(월) — 신선
STALE = {"picks": [{"symbol": "005930", "rank": 1}],
         "score_reference_date": "2026-07-18"}   # 낡은 신호
EMPTY = {"picks": [], "score_reference_date": "2026-07-21"}

CTX = EntryContext(symbol="005930", name="삼성전자", market="kospi",
                   audit_info="정상", state="",
                   signal_price=100_000, avg_trading_value_krw=10**12)


def _md(price: int) -> MarketData:
    q = Quote(symbol="005930", name="삼성전자", price=price,
              change_rate=Decimal("0"), volume=0)
    return MarketData(quote=q, bid=price - 100, ask=price + 100)


def _bpos(qty=9, avg=100_050) -> Position:
    return Position(symbol="005930", name="삼성전자", quantity=qty,
                    avg_price=avg, current_price=avg, eval_amount=avg * qty)


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
    """quotes는 호출 순서 스크립트(소진 시 마지막 반복 — 서비스 루프의
    사이클 수 비결정성 흡수, test_service.FakeBroker 관례)."""

    def __init__(self, quotes, balances=None):
        self._quotes = list(quotes)
        self._last = {}
        self._balances = list(balances or [Balance((), 0, 0)])
        self.placed = []

    async def get_quotes(self, symbols):
        book = self._quotes.pop(0) if self._quotes else self._last
        self._last = book
        return [book[s] for s in symbols if s in book]

    async def place_order(self, req):
        self.placed.append(req)
        return OrderAck(order_no=f"ORD{len(self.placed)}", message="ok")

    async def cancel_order(self, order_no, symbol):
        return OrderAck(order_no=f"CXL{order_no}", message="cancelled")

    async def get_open_orders(self):
        return []

    async def get_balance(self):
        if len(self._balances) > 1:
            return self._balances.pop(0)
        return self._balances[0]

    async def get_deposit(self):
        return Deposit(total=10_000_000, available=10_000_000)


class _Analysis:
    """호출 순서대로 결과를 돌려주는 분석 스텁(마지막 값 반복) — '분석의
    늦은 도착'을 사이클 단위로 재현하고 호출 횟수로 래치를 검증한다."""

    def __init__(self, seq):
        self._seq = list(seq)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if len(self._seq) > 1:
            return self._seq.pop(0)
        return self._seq[0]


class _Store(TradingStore):
    def __init__(self, engine, contexts):
        super().__init__(engine, now=lambda: T0)
        self._contexts = contexts

    def entry_context(self, symbols, signal_date, avg_days=20):
        return {s: c for s, c in self._contexts.items() if s in symbols}

    def instrument_state(self, symbol):
        return None


async def _yield_sleep(_):
    await asyncio.sleep(0)


def _service(tmp_path, analysis, broker, hours):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'latch.db'}")
    Base.metadata.create_all(engine)
    store = _Store(engine, {"005930": CTX})
    return TradingService(broker, broker, store, CFG, _Cal(hours), analysis,
                          sleep=_yield_sleep, now=lambda: T0), store


HAPPY_BOOK = {"005930": _md(100_000)}


# ── 순수(selection) — kind 분류 ─────────────────────────────────────────

def test_가격_결측은_technical_그외는_strategic():
    missing = EntryCandidate(symbol="005930", name="s", market="kospi",
                             signal_price=100_000, current_price=0,
                             audit_info="정상", state="",
                             avg_trading_value_krw=10**12)
    gapped = EntryCandidate(symbol="000660", name="h", market="kospi",
                            signal_price=100_000, current_price=200_000,
                            audit_info="정상", state="",
                            avg_trading_value_krw=10**12)
    result = select_entries([missing, gapped], set(), 10_000_000, CFG)
    kinds = {d.symbol: d.kind for d in result.dropped}
    assert kinds["005930"] is DropKind.TECHNICAL
    assert kinds["000660"] is DropKind.STRATEGIC


# ── 서비스 — 래치 4분기 ─────────────────────────────────────────────────

@pytest.mark.anyio
async def test_분석_늦은_도착이면_창_내_재시도로_진입한다(tmp_path):
    """P5 결함 재현+수정 검증: 1사이클 분석 부재 → 래치 미세팅 → 2사이클
    분석 도착 → 진입 성공. 경고는 중복 없이 1회."""
    analysis = _Analysis([None, FRESH])
    broker = _Broker(quotes=[HAPPY_BOOK], balances=[Balance((_bpos(),), 0, 0)])
    service, store = _service(tmp_path, analysis, broker,
                              [True, True, False])
    await service.run()
    buys = [r for r in broker.placed if r.side is OrderSide.BUY]
    assert len(buys) == 1                      # 재시도 사이클에서 진입 성사
    assert analysis.calls >= 2
    retry_warns = [w for w in service.progress().warnings
                   if "no analysis result yet" in w]
    assert len(retry_warns) == 1               # warn_once 중복 억제


@pytest.mark.anyio
async def test_낡은_신호는_재시도_후_신선_도착시_진입(tmp_path):
    analysis = _Analysis([STALE, FRESH])
    broker = _Broker(quotes=[HAPPY_BOOK], balances=[Balance((_bpos(),), 0, 0)])
    service, _ = _service(tmp_path, analysis, broker, [True, True, False])
    await service.run()
    assert len([r for r in broker.placed if r.side is OrderSide.BUY]) == 1


@pytest.mark.anyio
async def test_신선한_픽_0은_판정_성립_래치(tmp_path):
    """risk_off 픽 0은 정상 판정 — 재시도하지 않는다(호출 1회로 고정)."""
    analysis = _Analysis([EMPTY])
    broker = _Broker(quotes=[HAPPY_BOOK])
    service, _ = _service(tmp_path, analysis, broker, [True, True, False])
    await service.run()
    assert broker.placed == []
    assert analysis.calls == 1                 # 2사이클째는 래치로 미호출


@pytest.mark.anyio
async def test_전_후보_기술_드롭이면_재시도_quote_도착시_진입(tmp_path):
    """1사이클 빈 quote(전 후보 기술 드롭) → 래치 미세팅 → 2사이클 quote
    도착 → 진입(스펙 §4-c ③ — degenerate quote 전례)."""
    analysis = _Analysis([FRESH])
    broker = _Broker(quotes=[{}, HAPPY_BOOK],
                     balances=[Balance((_bpos(),), 0, 0)])
    service, _ = _service(tmp_path, analysis, broker, [True, True, False])
    await service.run()
    assert len([r for r in broker.placed if r.side is OrderSide.BUY]) == 1
    retry_warns = [w for w in service.progress().warnings
                   if "technical reasons" in w]
    assert len(retry_warns) == 1


@pytest.mark.anyio
async def test_혼합_드롭은_즉시_래치(tmp_path):
    """technical(quote 부재)+strategic(갭 가드) 공존 — strategic 판정이
    성립했으므로 재시도 없이 래치(독스트링 "혼합 → True" 회귀)."""
    fresh2 = {"picks": [{"symbol": "005930", "rank": 1},
                        {"symbol": "000660", "rank": 2}],
              "score_reference_date": "2026-07-21"}
    analysis = _Analysis([fresh2])
    # 005930은 갭 가드 탈락(현재가 2배), 000660은 quote 자체 부재
    broker = _Broker(quotes=[{"005930": _md(200_000)}])
    service, _ = _service(tmp_path, analysis, broker, [True, True, False])
    await service.run()
    assert broker.placed == []
    assert analysis.calls == 1                 # 래치 — 재시도 없음


@pytest.mark.anyio
async def test_시딩된_래치는_진입_시도_자체를_차단(tmp_path):
    """Task 1↔Task 2 상호작용 회귀(계획 Task 2 "테스트로 고정"): 당일 매수
    주문이 DB에 있으면(_seed_daily_caps의 has_buy 시딩) 재시도 계약과
    무관하게 _enter_positions 호출 자체가 게이트된다 — 분석 스텁 호출 0회.
    재시도 사이클과 기발주 주문의 이중 진입이 구조적으로 불가함의 증명."""
    analysis = _Analysis([FRESH])
    broker = _Broker(quotes=[HAPPY_BOOK])
    service, store = _service(tmp_path, analysis, broker,
                              [True, True, False])
    prior = store.create_run("{}", "mock")
    store.finish_run(prior, "failed", failure_reason="crash")
    store.record_order(prior, None, order_no="P1", symbol="005930",
                       side="buy", order_style="limit", req_price=100_000,
                       req_qty=1, status="filled",
                       resp_body={"ord_no": "P1", "return_msg": "ok"},
                       est_krw=100_000)
    await service.run()
    assert analysis.calls == 0                 # 진입 배치 미착수
    assert broker.placed == []


@pytest.mark.anyio
async def test_전략_탈락은_래치_재시도_없음(tmp_path):
    """갭 가드 탈락(현재가 2배) — 판정 성립: 분석 재조회 없이 래치 유지,
    발주된 주문과의 중복 진입도 구조적으로 불가(호출 1회)."""
    analysis = _Analysis([FRESH])
    broker = _Broker(quotes=[{"005930": _md(200_000)}])
    service, _ = _service(tmp_path, analysis, broker, [True, True, False])
    await service.run()
    assert broker.placed == []
    assert analysis.calls == 1
    assert any("gap guard" in w for w in service.progress().warnings)
