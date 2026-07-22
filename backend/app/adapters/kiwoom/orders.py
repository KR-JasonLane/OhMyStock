"""키움 주문 TR의 요청 빌더·응답 파서 — broker.py 비대화 방지(계획서 Task 4).

실측 근거(2026-07-22, CLAUDE.md §5) — G2(매수+지정가)·G3(매수/매도 시장가)·
틱 판별로 확정. SELL+LIMIT(kt10001, trde_tp="0"+ord_uv)은
라이브 마커(tests/live/test_live_orders.py)로 **실측 완료**(2026-07-22 12:08
장중: rc=0 '모의투자 매도주문완료' — broker-api 패널 공백 해소):
  - category "ordr": 매수 kt10000 / 매도 kt10001 / 취소 kt10003
  - 바디 {dmst_stex_tp:"KRX", stk_cd:<맨코드>, ord_qty:<str>, trde_tp, ord_uv(지정가만)}
  - trde_tp는 **단일자리**: "0"=지정가, "3"=시장가 (두자리 "00"/"03"은 오류 — 실측 정정)
  - 계좌번호/비밀번호 필드 없음(appkey 귀속)
  - 응답 {ord_no, return_msg}; 취소는 {orig_ord_no, cncl_qty:"0"=전량}
  - 미체결 ka10075(category "acnt"): 바디 {all_stk_tp:"0", trde_tp:"0", stex_tp:"0"},
    리스트 키 "oso", 행 ord_no/stk_cd/ord_qty/oso_qty/ord_pric/ord_stt/io_tp_nm
  - 호가단위 위반은 rc=20 / RC4003 (틱 판별 실측)

키움 코드값이 이 모듈 밖(도메인)으로 새지 않는다 — 도메인은 OrderSide/OrderStyle
enum만 안다(스펙 §5, 브로커 교체 가능성)."""

from app.domain.broker import OpenOrder, OrderRequest
from app.domain.trading.models import OrderSide, OrderStyle

CATEGORY_ORDER = "ordr"
CATEGORY_ACCOUNT = "acnt"

API_BUY = "kt10000"
API_SELL = "kt10001"
API_CANCEL = "kt10003"
API_OPEN_ORDERS = "ka10075"

# 도메인 enum → 키움 trde_tp (단일자리 — G2 실측 확정)
_TRDE_TP = {OrderStyle.LIMIT: "0", OrderStyle.MARKET: "3"}
# 도메인 방향 → 주문 TR (키움은 방향을 TR 선택으로 구분)
_ORDER_API = {OrderSide.BUY: API_BUY, OrderSide.SELL: API_SELL}


def order_api_id(side: OrderSide) -> str:
    return _ORDER_API[side]


def build_order_body(req: OrderRequest) -> dict:
    """kt10000/kt10001 요청 바디. ord_uv는 지정가에만 넣는다(G2: 시장가는
    ord_uv 없이 접수됨 — G3 실측)."""
    body = {"dmst_stex_tp": "KRX", "stk_cd": req.symbol,
            "ord_qty": str(req.quantity), "trde_tp": _TRDE_TP[req.style]}
    if req.style is OrderStyle.LIMIT:
        body["ord_uv"] = str(req.limit_price)
    return body


def build_cancel_body(order_no: str, symbol: str) -> dict:
    """kt10003 — cncl_qty="0"=잔량 전량 취소(G2 실측: orig_ord_no/cncl_qty가
    유효 필드명, 커뮤니티 소스의 org_ord_no/ord_qty는 오류)."""
    return {"dmst_stex_tp": "KRX", "orig_ord_no": order_no,
            "stk_cd": symbol, "cncl_qty": "0"}


OPEN_ORDERS_BODY = {"all_stk_tp": "0", "trde_tp": "0", "stex_tp": "0"}


def parse_open_order(row: dict, normalize_symbol, to_int, to_price) -> OpenOrder:
    """ka10075 oso 행 → OpenOrder. 파싱 헬퍼(_normalize_symbol 등)는 broker.py
    소유라 주입받는다(중복 정의 방지). io_tp_nm 원문 값은 라이브 마커로 실측
    확정(2026-07-22): 매도 지정가 행에서 `'-매도'` — 접두 기호가 붙는 **부분
    문자열**이라 정확-일치가 아닌 `in` 판정이 필수(방어적 설계 적중, broker-api
    패널). 미지 값은 fail-loud."""
    io = row.get("io_tp_nm", "")
    if "매수" in io:
        side = OrderSide.BUY
    elif "매도" in io:
        side = OrderSide.SELL
    else:
        raise ValueError(f"unknown io_tp_nm in open order: {io!r}")
    return OpenOrder(
        order_no=row["ord_no"],
        symbol=normalize_symbol(row["stk_cd"]),
        side=side,
        order_qty=to_int(row.get("ord_qty")),
        unfilled_qty=to_int(row.get("oso_qty")),
        order_price=to_price(row.get("ord_pric")),
        status=row.get("ord_stt", ""),
    )
