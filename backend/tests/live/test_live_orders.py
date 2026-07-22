"""P5 Task 4 라이브 마커 — 주문 조합별 실측(계획서 Task 4 Files 요구).

실행(장중): uv run pytest -m live tests/live/test_live_orders.py -v

목적(broker-api 패널 Important #1/#3):
  - **SELL+LIMIT**(kt10001, trde_tp="0"+ord_uv) 접수 — G2(매수+지정가)/G3(시장가
    양방향)가 못 덮은 조합. 안전 설계: 시장가 매수 1주 → 체결불가 고가 매도
    지정가 접수 확인 → 취소 → 시장가 매도 청산(포지션 방치 없음).
  - **io_tp_nm 원문 값 로깅** — 필드 존재만 실측됐던 값 포맷 확정.

⚠️ 실주문(모의계좌). 백엔드 동시 가동 금지(앱키당 1토큰 — CLAUDE.md §5)."""

import asyncio

import httpx
import pytest

from app.adapters.kiwoom.broker import KiwoomBroker
from app.adapters.kiwoom.client import KiwoomHttpClient
from app.core.config import Settings
from app.core.market_calendar import is_market_hours
from app.domain.broker import OrderRequest, OrderSide, OrderStyle
from app.domain.trading.ticks import round_to_tick

pytestmark = pytest.mark.live

PROBE = "005930"


@pytest.fixture
def settings() -> Settings:
    s = Settings()
    if not s.kiwoom_mock:
        pytest.skip("라이브 스모크는 모의서버(KIWOOM_MOCK=true)에서만 실행한다")
    if not is_market_hours():
        pytest.skip("주문 실측은 장중(09:00~15:30 거래일)에만 유효하다")
    return s


@pytest.mark.anyio
async def test_live_매도지정가_접수와_io_tp_nm_실측(settings):
    """SELL+LIMIT rc=0 접수 + ka10075 io_tp_nm 원문 확정. 전 경로 정리 보장."""
    client = KiwoomHttpClient(settings)
    broker = KiwoomBroker(client)
    bought = False
    sell_ord_no: str | None = None
    try:
        # 1) 시장가 매수 1주 — 포지션 생성(매도 지정가 접수의 전제)
        ack = await broker.place_order(OrderRequest(
            symbol=PROBE, side=OrderSide.BUY, style=OrderStyle.MARKET, quantity=1))
        bought = True
        print(f"[매수 접수] ord_no={ack.order_no} msg={ack.message!r}")
        await asyncio.sleep(1.5)  # 체결 반영

        # 2) SELL+LIMIT — 현재가 +10% 고가(체결 불가, 틱 스냅) 접수 확인
        cur = (await broker.get_quote(PROBE)).price
        high_px = round_to_tick(int(cur * 1.10), "kospi", "up")
        ack_sell = await broker.place_order(OrderRequest(
            symbol=PROBE, side=OrderSide.SELL, style=OrderStyle.LIMIT,
            quantity=1, limit_price=high_px))
        sell_ord_no = ack_sell.order_no
        print(f"[SELL+LIMIT 접수 ✅] ord_no={sell_ord_no} px={high_px:,} "
              f"msg={ack_sell.message!r}")  # ← 이 rc=0이 미실측 조합의 실측
        assert sell_ord_no

        # 3) io_tp_nm 원문 로깅(미실측 값 포맷 확정) — get_open_orders 파싱도 검증
        raw, _, _ = await client.call("acnt", "ka10075",
                                      {"all_stk_tp": "0", "trde_tp": "0",
                                       "stex_tp": "0"})
        for row in raw.get("oso") or []:
            print(f"[io_tp_nm 원문] ord_no={row.get('ord_no')!r} "
                  f"io_tp_nm={row.get('io_tp_nm')!r} ord_stt={row.get('ord_stt')!r}")
        parsed = await broker.get_open_orders()
        mine = [o for o in parsed if o.order_no == sell_ord_no]
        assert mine and mine[0].side is OrderSide.SELL  # 파서의 매도 판정 실측 검증

        # 4) 매도 지정가 취소
        cancel = await broker.cancel_order(sell_ord_no, PROBE)
        sell_ord_no = None
        print(f"[취소] ord_no={cancel.order_no} msg={cancel.message!r}")
    finally:
        # 안전정리: 미취소 매도 지정가 → 취소, 보유 → 시장가 청산
        try:
            if sell_ord_no:
                await broker.cancel_order(sell_ord_no, PROBE)
                print("[안전정리] 매도 지정가 취소")
            if bought:
                await asyncio.sleep(0.5)
                bal = await broker.get_balance()
                if any(p.symbol == PROBE and p.quantity > 0 for p in bal.positions):
                    await broker.place_order(OrderRequest(
                        symbol=PROBE, side=OrderSide.SELL,
                        style=OrderStyle.MARKET, quantity=1))
                    print("[안전정리] 시장가 매도 청산")
        finally:
            await broker.aclose()
