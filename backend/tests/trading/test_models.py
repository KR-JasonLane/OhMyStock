"""models.py — 상태기계 enum 완결성 + TradePosition 계약."""

from datetime import datetime, timezone

import pytest

from app.domain.trading.models import (EntryPhase, ExitEvaluation, ExitPhase,
                                       ExitReason, Fill, Order, OrderSide,
                                       OrderStyle, PositionState, TradePosition)


def test_상태기계_enum이_스펙_6_1과_일치한다():
    assert {s.value for s in PositionState} == {
        "pending_entry", "entered", "exiting", "closed",
        "entry_failed", "exit_failed"}
    assert {p.value for p in EntryPhase} == {
        "limit_submitted", "cancel_requested", "market_submitted"}
    # ExitPhase는 익절 지정가 경로만 — CANCEL_REQUESTED 없음(시장가 폴백은
    # 취소 후 즉시 재발주가 아니라 §6-2-b 타임아웃 흐름)
    assert {p.value for p in ExitPhase} == {"limit_submitted", "market_submitted"}


def test_청산_사유가_우선순위_전체를_커버한다():
    # §6-2 우선순위 0~3 + 킬스위치(§8-1-b)
    assert {r.value for r in ExitReason} == {
        "max_holding", "stop_loss", "trailing_stop", "take_profit", "kill_switch"}


def test_주문_유형은_도메인_중립_enum():
    # 키움 trde_tp 코드값("0"/"3")은 여기 없어야 한다 — 어댑터 소관(§5)
    assert {s.value for s in OrderStyle} == {"limit", "market"}
    assert {s.value for s in OrderSide} == {"buy", "sell"}


def _pos(**overrides) -> TradePosition:
    base = dict(symbol="005930", name="삼성전자", market="kospi",
                state=PositionState.ENTERED, entry_price=272_750,
                quantity=1, peak_price=272_750, trailing_active=False,
                entered_at=datetime(2026, 7, 22, tzinfo=timezone.utc))
    return TradePosition(**{**base, **overrides})


def test_TradePosition은_불변이고_원단위_int():
    pos = _pos()
    assert isinstance(pos.entry_price, int)  # G3 실측: 원 단위 정수 확정
    assert pos.market == "kospi"  # 비용 계산용(트레이더 패널 — 조달처 명확화)
    assert pos.exit_reason is None and pos.realized_pnl is None
    with pytest.raises(AttributeError):
        pos.peak_price = 999  # frozen


def test_TradePosition_sanity_검증():
    # 브로커 응답 파싱 실수가 조용히 통과하지 않는다(개발자 패널)
    with pytest.raises(ValueError, match="quantity"):
        _pos(quantity=0)
    with pytest.raises(ValueError, match="prices"):
        _pos(entry_price=0)
    with pytest.raises(ValueError, match="peak_price < entry"):
        _pos(peak_price=200_000)  # ENTERED에서 peak < entry는 추적 오류


def test_Order_기본_생성과_sanity():
    order = Order(order_no="0034447", symbol="005930", side=OrderSide.BUY,
                  style=OrderStyle.LIMIT, req_price=246_000, req_qty=1,
                  status="submitted",
                  created_at=datetime(2026, 7, 22, tzinfo=timezone.utc))
    assert order.order_no == "0034447"  # G2 실측 ord_no 형태
    with pytest.raises(ValueError, match="req_qty"):
        Order(order_no="1", symbol="005930", side=OrderSide.BUY,
              style=OrderStyle.MARKET, req_price=0, req_qty=0,
              status="submitted",
              created_at=datetime(2026, 7, 22, tzinfo=timezone.utc))


def test_Fill_기본_생성과_sanity():
    # Order/TradePosition과 대칭(개발자 델타 — 검증 로직 있는데 미커버 방지)
    fill = Fill(order_no="0034447", fill_price=272_750, fill_qty=1,
                filled_at=datetime(2026, 7, 22, tzinfo=timezone.utc))
    assert fill.fill_price == 272_750
    with pytest.raises(ValueError, match="fill_qty"):
        Fill(order_no="1", fill_price=272_750, fill_qty=0,
             filled_at=datetime(2026, 7, 22, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="fill_price"):
        Fill(order_no="1", fill_price=0, fill_qty=1,
             filled_at=datetime(2026, 7, 22, tzinfo=timezone.utc))


def test_ExitEvaluation은_판정_반환_계약이다():
    # Task 3 evaluate_exit ↔ Task 6b monitor가 공유하는 SSOT(개발자 패널 —
    # prose 주석이 아니라 코드 타입으로)
    ev = ExitEvaluation(reason=None, new_peak=280_000, new_trailing_active=True)
    assert ev.reason is None and ev.new_peak == 280_000
    held = ExitEvaluation(reason=ExitReason.STOP_LOSS, new_peak=272_750,
                          new_trailing_active=False)
    assert held.reason is ExitReason.STOP_LOSS
