"""EntryExecutor(6a) — 상태기계 전 경로를 fake OrderPort로 검증.
sleep 주입으로 결정적(실시간 대기 없음).

폴링 계약(트레이더 C1 반영): timeout=3.0/interval=1.0 기준 유예 sleep 1회 후
폴 3회. 부재(None)는 '한 번도 관측 못 한 주문'이면 연속 2회 확인 후에만
체결로 판정된다 — 시나리오 스크립트는 이 계약 기준으로 작성."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.domain.broker import MarketData, OrderSide, OrderStyle, Quote
from tests.trading.conftest import FakeOrderPortBase
from app.domain.trading.config import TradingConfig
from app.domain.trading.entry import EntryExecutor
from app.domain.trading.models import EntryPhase, PositionState
from app.domain.trading.selection import EntryPlan

CFG = TradingConfig(max_single_order_krw=10_000_000, max_daily_orders=50,
                    max_daily_order_krw=50_000_000, min_avg_trading_value_krw=0,
                    limit_order_timeout_sec=3.0, poll_interval_sec=1.0)
PLAN = EntryPlan(symbol="005930", name="삼성전자", market="kospi",
                 quantity=10, budget_krw=3_000_000)
T0 = datetime(2026, 7, 22, 0, 30, tzinfo=timezone.utc)


class FakeOrders(FakeOrderPortBase):
    """공용 베이스(conftest — 개발자 P5-T6b #4) + 6a 전용 get_quotes.
    requote_ask: 재조회 응답 ask. None=조회 실패(stale 폴백 경로).
    poll_calls는 베이스 calls에서 파생."""

    def __init__(self, open_orders_script=None, cancel_script=None,
                 requote_ask=None):
        super().__init__(open_orders_script, cancel_script)
        self._requote_ask = requote_ask

    @property
    def poll_calls(self) -> int:
        return sum(1 for c in self.calls if c[0] == "open_orders")

    async def get_quotes(self, symbols):
        if self._requote_ask is None:
            raise ConnectionError("quote unavailable")  # stale 폴백 경로
        q = Quote(symbol=symbols[0], name="삼성전자", price=self._requote_ask,
                  change_rate=Decimal("0"), volume=0)
        return [MarketData(quote=q, bid=self._requote_ask - 500,
                           ask=self._requote_ask)]


async def _no_sleep(_): return None


def _executor(fake, caps=None, persist=None, on_order=None) -> EntryExecutor:
    return EntryExecutor(fake, CFG, caps or (lambda *_: None),
                         persist_phase=persist, on_order=on_order,
                         sleep=_no_sleep, now=lambda: T0)


@pytest.mark.anyio
async def test_지정가_전량_체결():
    # 미관측 주문의 부재는 연속 2회 확인 후 체결 판정(C1) — None 2개 필요
    fake = FakeOrders(open_orders_script=[None, None])
    phases = []
    out = await _executor(fake, persist=phases.append).execute(PLAN, ask=273_500)
    pos = out.position
    assert pos is not None and pos.state is PositionState.ENTERED
    assert pos.quantity == 10 and pos.entry_price == 273_500  # offset 0 = ask
    assert pos.peak_price == pos.entry_price and pos.entered_at == T0
    assert out.requires_reconcile is False
    assert phases == [EntryPhase.LIMIT_SUBMITTED]
    assert len(fake.placed) == 1 and fake.placed[0].style is OrderStyle.LIMIT
    assert fake.cancelled == []


@pytest.mark.anyio
async def test_지정가는_틱_스냅과_offset을_적용():
    cfg = TradingConfig(max_single_order_krw=10_000_000, max_daily_orders=50,
                        max_daily_order_krw=50_000_000,
                        min_avg_trading_value_krw=0, entry_tick_offset=2,
                        limit_order_timeout_sec=3.0)
    fake = FakeOrders(open_orders_script=[None, None])
    ex = EntryExecutor(fake, cfg, lambda *_: None, sleep=_no_sleep,
                       now=lambda: T0)
    await ex.execute(PLAN, ask=273_500)
    # 273,500 − 2틱(500×2) = 272,500 (유효 호가)
    assert fake.placed[0].limit_price == 272_500


@pytest.mark.anyio
async def test_첫_폴_부재는_체결로_속단하지_않는다():
    """C1 회귀 — 발주 직후 미체결 시스템 미전파로 첫 폴에 주문이 안 보여도
    '전량 체결'로 오판하지 않는다(유령 포지션 방지). 이후 관측된 주문이
    타임아웃까지 미체결 → 정상 취소·폴백 경로."""
    # 지정가: 부재(미전파) → 10 관측 → 10 → 타임아웃 → 취소 → 시장가:
    # 부재 연속 2회(미관측 주문 확인 규칙) → 체결
    fake = FakeOrders(open_orders_script=[None, 10, 10, None, None])
    out = await _executor(fake).execute(PLAN, ask=273_500)
    assert out.position is not None and out.position.quantity == 10
    assert fake.cancelled == ["ORD1"]  # 첫 부재에서 체결 판정했다면 취소 없음
    assert [r.style for r in fake.placed] == [OrderStyle.LIMIT, OrderStyle.MARKET]
    assert fake.poll_calls == 5


@pytest.mark.anyio
async def test_부분체결은_체결분만_인정하고_시장가_재발주_없음():
    # 타임아웃(3s, 1s 폴링)까지 잔량 4 유지 → 취소 → filled 6 인정(§6-1)
    fake = FakeOrders(open_orders_script=[4, 4, 4])
    phases = []
    out = await _executor(fake, persist=phases.append).execute(PLAN, ask=273_500)
    assert out.position is not None and out.position.quantity == 6
    assert phases == [EntryPhase.LIMIT_SUBMITTED, EntryPhase.CANCEL_REQUESTED]
    assert len(fake.placed) == 1  # 시장가 재발주 없음
    assert fake.cancelled == ["ORD1"]


@pytest.mark.anyio
async def test_전량_미체결은_취소_후_시장가_폴백():
    # 지정가 잔량 10 유지 → 취소 → 시장가 → 관측 후 소멸 = 체결
    fake = FakeOrders(open_orders_script=[10, 10, 10, 10, None])
    phases = []
    out = await _executor(fake, persist=phases.append).execute(PLAN, ask=273_500)
    assert out.position is not None and out.position.quantity == 10
    # 재조회 실패(fake 기본) → stale ask 폴백 = 관측 ask
    assert out.position.entry_price == 273_500
    assert phases == [EntryPhase.LIMIT_SUBMITTED, EntryPhase.CANCEL_REQUESTED,
                      EntryPhase.MARKET_SUBMITTED]
    assert [r.style for r in fake.placed] == [OrderStyle.LIMIT, OrderStyle.MARKET]


@pytest.mark.anyio
async def test_시장가_재발주는_시세를_재조회해_caps와_평단에_반영():
    """트레이더 I1 — 지정가 타임아웃 동안 낡은 ask 대신 재조회 값으로
    caps 재검증·평단 추정."""
    calls = []
    fake = FakeOrders(open_orders_script=[10, 10, 10, None, None],
                      requote_ask=280_000)
    out = await _executor(
        fake, caps=lambda amount, side: calls.append(amount)
    ).execute(PLAN, ask=273_500)
    assert calls == [273_500 * 10, 280_000 * 10]  # 시장가 caps는 fresh ask
    assert out.position is not None
    assert out.position.entry_price == 280_000  # 평단 추정도 fresh ask


@pytest.mark.anyio
async def test_시장가_부분체결은_체결분만_인정하고_잔여_취소():
    # 시장가 잔량 3 → filled 7 인정 + 잔여 취소(감시 밖 지연 체결 방지 —
    # 개발자 #3/트레이더 I3)
    fake = FakeOrders(open_orders_script=[10, 10, 10, 10, 3, 3])
    out = await _executor(fake).execute(PLAN, ask=273_500)
    assert out.position is not None and out.position.quantity == 7
    assert out.requires_reconcile is False
    assert fake.cancelled == ["ORD1", "ORD2"]  # 지정가 취소 + 시장가 잔여 취소


@pytest.mark.anyio
async def test_시장가도_미체결이면_ENTRY_FAILED와_잔여_취소():
    fake = FakeOrders(open_orders_script=[10] * 3 + [10] * 3)
    out = await _executor(fake).execute(PLAN, ask=273_500)
    assert out.position is None and "unfilled" in out.failure_reason
    assert out.requires_reconcile is False  # 취소 성공 — 상태 확정
    assert len(fake.cancelled) == 2  # 지정가 취소 + 시장가 잔여 취소


@pytest.mark.anyio
async def test_지정가_취소_실패는_시장가_재발주_없이_reconcile로():
    """이중 매수 가드(트레이더 C1/I4) — 취소 실패는 주문 상태 불명(직전 체결
    가능). 시장가 폴백 강행 시 이중 포지션 위험 → 중단 + reconcile 위임."""
    fake = FakeOrders(open_orders_script=[10, 10, 10],
                      cancel_script=[RuntimeError("cancel rejected")])
    out = await _executor(fake).execute(PLAN, ask=273_500)
    assert out.position is None and out.requires_reconcile is True
    assert "cancel failed" in out.failure_reason
    assert [r.style for r in fake.placed] == [OrderStyle.LIMIT]  # 폴백 중단


@pytest.mark.anyio
async def test_시장가_잔여취소_실패는_reconcile_표식():
    """트레이더 I3 — 취소 실패는 '주문이 여전히 살아있을 수 있음'이라는
    확정적 신호. 일반 미체결 실패와 구분해 즉시 대조 대상으로 표식."""
    fake = FakeOrders(open_orders_script=[10] * 3 + [10] * 3,
                      cancel_script=[None, RuntimeError("boom")])
    out = await _executor(fake).execute(PLAN, ask=273_500)
    assert out.position is None and out.requires_reconcile is True
    assert "may still be live" in out.failure_reason


@pytest.mark.anyio
async def test_시장가_부분체결_잔여취소_실패도_reconcile_표식():
    # 포지션은 인정(체결분 감시 유지)하되 잔여 주문 생존 가능 → 즉시 대조
    fake = FakeOrders(open_orders_script=[10, 10, 10, 10, 3, 3],
                      cancel_script=[None, RuntimeError("boom")])
    out = await _executor(fake).execute(PLAN, ask=273_500)
    assert out.position is not None and out.position.quantity == 7
    assert out.requires_reconcile is True


@pytest.mark.anyio
async def test_관측_전무면_보수적으로_실패_표면화():
    # 조회가 계속 실패 — 체결 여부 불명. 허위 ENTERED 금지, reconcile로 위임
    err = ConnectionError("down")
    fake = FakeOrders(open_orders_script=[err] * 12)
    out = await _executor(fake).execute(PLAN, ask=273_500)
    assert out.position is None
    # 구조화 필드로 분기(개발자 #2 — 문자열 매칭 금지) + 즉시 대조(트레이더 I2)
    assert out.requires_reconcile is True


@pytest.mark.anyio
async def test_caps는_발주_직전_호출되고_거부시_주문_안나감():
    calls = []

    def caps(amount, side):
        calls.append((amount, side))
        raise ValueError("single-order cap exceeded")

    fake = FakeOrders()
    with pytest.raises(ValueError, match="cap exceeded"):
        await _executor(fake, caps=caps).execute(PLAN, ask=273_500)
    # 지정가×수량 — 발주 직전 검증(§8-1) + side=BUY 전달(P5-T6b 트레이더 C2)
    assert calls == [(273_500 * 10, OrderSide.BUY)]
    assert fake.placed == []  # 주문이 아예 안 나감


@pytest.mark.anyio
async def test_on_order가_주문_감사를_받는다():
    fake = FakeOrders(open_orders_script=[None, None])
    audit = []
    out = await _executor(
        fake, on_order=lambda ack, req, st: audit.append((ack.order_no, st))
    ).execute(PLAN, ask=273_500)
    assert out.position is not None
    assert audit == [("ORD1", "submitted")]


@pytest.mark.anyio
async def test_잘못된_ask는_즉시_실패():
    out = await _executor(FakeOrders()).execute(PLAN, ask=0)
    assert out.position is None and "invalid ask" in out.failure_reason


@pytest.mark.anyio
async def test_시장가_경로에서도_caps_거부시_주문_안나감():
    # 두 번째 caps 호출(시장가, §8-1 마지막 방어선) 회귀(보안 Minor)
    calls = []

    def caps(amount, side):
        calls.append(amount)
        if len(calls) == 2:
            raise ValueError("daily cap exceeded")

    fake = FakeOrders(open_orders_script=[10] * 3)  # 지정가 전량 미체결 → 폴백
    with pytest.raises(ValueError, match="daily cap"):
        await _executor(fake, caps=caps).execute(PLAN, ask=273_500)
    assert len(calls) == 2
    assert [r.style for r in fake.placed] == [OrderStyle.LIMIT]  # 시장가 미발주


@pytest.mark.anyio
async def test_감사_콜백_예외는_격리되고_흐름은_계속():
    """주문이 이미 나간 뒤의 기록 실패가 폴링·포지션 추적을 죽이면 "주문은
    나갔는데 추적 끊김" 최악 상태(보안 #3) — 격리 후 error 로그만."""
    def boom(*_): raise RuntimeError("db down")

    fake = FakeOrders(open_orders_script=[None, None])
    out = await _executor(fake, on_order=boom).execute(PLAN, ask=273_500)
    assert out.position is not None  # 흐름 계속 — 포지션 추적 유지


@pytest.mark.anyio
async def test_시장가_잔여취소도_감사를_남긴다():
    fake = FakeOrders(open_orders_script=[10] * 3 + [10] * 3)
    audit = []
    await _executor(
        fake, on_order=lambda ack, req, st: audit.append((st, req.style))
    ).execute(PLAN, ask=273_500)
    # 지정가 제출/취소 + 시장가 제출/잔여취소 — 전 주문 감사(보안 #1)
    assert [a[0] for a in audit] == ["submitted", "cancelled",
                                    "submitted", "cancelled"]
