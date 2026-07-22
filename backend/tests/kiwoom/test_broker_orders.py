"""OrderPort 키움 구현(P5 Task 4) — 요청 바디·응답 파싱을 G1/G2 실측 필드로
검증한다. respx로 HTTP를 스텁하고 실제 요청 바디를 캡처해 실측 계약과 대조."""

import json

import pytest
import respx

from app.adapters.kiwoom.broker import KiwoomBroker
from app.adapters.kiwoom.client import KiwoomHttpClient
from app.core.config import Settings
from app.domain.broker import OrderRequest
from app.domain.errors import ApiError, BrokerError
from app.domain.trading.models import OrderSide, OrderStyle

BASE = "https://mockapi.kiwoom.com"
TOKEN_JSON = {"token": "TOK", "token_type": "bearer",
              "expires_dt": "20991231235959", "return_code": 0, "return_msg": "ok"}

# G1 실측 필드(atn_stk_infr 행 — 발췌)
QUOTE_ROW = {"stk_cd": "005930", "stk_nm": "삼성전자", "cur_prc": "+273500",
             "flu_rt": "+5.60", "trde_qty": "1392658",
             "sel_bid": "+273500", "buy_bid": "+273000"}
EMPTY_ROW = {k: "" for k in QUOTE_ROW}  # 더미/결측 — G1: 빈 행으로 옴


async def _noop_sleep(_: float) -> None:
    return None


def _broker() -> KiwoomBroker:
    s = Settings(_env_file=None, kiwoom_app_key="AK", kiwoom_secret_key="SK",
                 kiwoom_mock=True, database_url="sqlite+pysqlite:///:memory:")
    return KiwoomBroker(KiwoomHttpClient(s, sleep=_noop_sleep))


def _mock_auth() -> None:
    respx.post(f"{BASE}/oauth2/token").respond(json=TOKEN_JSON)
    respx.post(f"{BASE}/oauth2/revoke").respond(json={"return_code": 0})


@pytest.mark.anyio
@respx.mock
async def test_get_quotes는_파이프_조인_요청과_빈행_필터():
    _mock_auth()
    route = respx.post(f"{BASE}/api/dostk/stkinfo").respond(
        json={"return_code": 0,
              "atn_stk_infr": [QUOTE_ROW, EMPTY_ROW]})
    broker = _broker()
    result = await broker.get_quotes(["005930", "000000"])
    body = json.loads(route.calls[0].request.content)
    assert body["stk_cd"] == "005930|000000"  # G1: 파이프 구분자(세미콜론 아님)
    assert len(result) == 1  # 빈 행 제외
    md = result[0]
    assert md.quote.symbol == "005930" and md.quote.price == 273_500
    assert md.bid == 273_000 and md.ask == 273_500  # buy_bid/sel_bid


@pytest.mark.anyio
@respx.mock
async def test_get_quotes는_100종목_초과를_청크_분할():
    _mock_auth()
    route = respx.post(f"{BASE}/api/dostk/stkinfo").respond(
        json={"return_code": 0, "atn_stk_infr": [QUOTE_ROW]})
    broker = _broker()
    await broker.get_quotes([f"{i:06d}" for i in range(150)])
    # G1: 최대 100종목(101→rc=5) — 150개는 100+50 두 번 호출
    assert route.call_count == 2
    first = json.loads(route.calls[0].request.content)["stk_cd"]
    second = json.loads(route.calls[1].request.content)["stk_cd"]
    assert first.count("|") == 99 and second.count("|") == 49


@pytest.mark.anyio
@respx.mock
async def test_place_order_지정가_바디는_G2_실측_필드():
    _mock_auth()
    route = respx.post(f"{BASE}/api/dostk/ordr").respond(
        json={"return_code": 0, "ord_no": "0034447",
              "return_msg": "모의투자 매수주문완료"})
    broker = _broker()
    ack = await broker.place_order(OrderRequest(
        symbol="005930", side=OrderSide.BUY, style=OrderStyle.LIMIT,
        quantity=1, limit_price=246_000))
    body = json.loads(route.calls[0].request.content)
    assert body == {"dmst_stex_tp": "KRX", "stk_cd": "005930",
                    "ord_qty": "1", "trde_tp": "0", "ord_uv": "246000"}
    assert route.calls[0].request.headers["api-id"] == "kt10000"
    assert ack.order_no == "0034447"


@pytest.mark.anyio
@respx.mock
async def test_place_order_시장가_매도는_ord_uv_없이_kt10001():
    _mock_auth()
    route = respx.post(f"{BASE}/api/dostk/ordr").respond(
        json={"return_code": 0, "ord_no": "0051403",
              "return_msg": "모의투자 매도주문완료"})
    broker = _broker()
    await broker.place_order(OrderRequest(
        symbol="005930", side=OrderSide.SELL, style=OrderStyle.MARKET, quantity=1))
    body = json.loads(route.calls[0].request.content)
    assert body["trde_tp"] == "3" and "ord_uv" not in body  # 단일자리(G2/G3 실측)
    assert route.calls[0].request.headers["api-id"] == "kt10001"


@pytest.mark.anyio
@respx.mock
async def test_place_order_호가단위_오류는_ApiError로_표면화():
    # 틱 판별 실측: rc=20 / RC4003 — client가 ApiError로 fail-loud
    _mock_auth()
    respx.post(f"{BASE}/api/dostk/ordr").respond(
        json={"return_code": 20,
              "return_msg": "[2000](RC4003:모의투자 호가단위 오류입니다.)"})
    broker = _broker()
    with pytest.raises(ApiError, match="RC4003"):
        await broker.place_order(OrderRequest(
            symbol="005930", side=OrderSide.BUY, style=OrderStyle.LIMIT,
            quantity=1, limit_price=244_750))


@pytest.mark.anyio
@respx.mock
async def test_cancel_order는_전량취소_바디():
    _mock_auth()
    route = respx.post(f"{BASE}/api/dostk/ordr").respond(
        json={"return_code": 0, "ord_no": "0034500",
              "return_msg": "모의투자 취소주문완료"})
    broker = _broker()
    ack = await broker.cancel_order("0034447", "005930")
    body = json.loads(route.calls[0].request.content)
    assert body == {"dmst_stex_tp": "KRX", "orig_ord_no": "0034447",
                    "stk_cd": "005930", "cncl_qty": "0"}  # G2: 전량취소 계약
    assert route.calls[0].request.headers["api-id"] == "kt10003"
    assert ack.order_no == "0034500"


@pytest.mark.anyio
@respx.mock
async def test_get_open_orders는_oso_행을_파싱():
    _mock_auth()
    respx.post(f"{BASE}/api/dostk/acnt").respond(
        json={"return_code": 0, "oso": [
            {"ord_no": "0034447", "stk_cd": "005930", "io_tp_nm": "-매수",
             "ord_qty": "000000000000001", "oso_qty": "000000000000001",
             "ord_pric": "000000000246000", "ord_stt": "접수"}]})
    broker = _broker()
    orders = await broker.get_open_orders()
    assert len(orders) == 1
    o = orders[0]
    assert o.order_no == "0034447" and o.symbol == "005930"
    assert o.side is OrderSide.BUY and o.unfilled_qty == 1
    assert o.order_price == 246_000 and o.status == "접수"


@pytest.mark.anyio
@respx.mock
async def test_get_open_orders_미체결_없으면_빈_리스트():
    _mock_auth()
    respx.post(f"{BASE}/api/dostk/acnt").respond(
        json={"return_code": 0, "oso": []})
    assert await _broker().get_open_orders() == []


@pytest.mark.anyio
@respx.mock
async def test_get_open_orders_페이지네이션이면_fail_loud():
    # cont-yn=Y — 조용한 누락 대신 예외(어댑터 docstring 계약)
    _mock_auth()
    respx.post(f"{BASE}/api/dostk/acnt").respond(
        headers={"cont-yn": "Y", "next-key": "K"},
        json={"return_code": 0, "oso": []})
    with pytest.raises(BrokerError, match="paginated"):
        await _broker().get_open_orders()


@pytest.mark.anyio
@respx.mock
async def test_get_quotes_degenerate와_편측호가_구분():
    """price=0(참 degenerate — 012510류)만 제외하고, 편측 호가 소진(상/하한가
    legit 케이스 — ask=0 또는 bid=0)은 유지한다(broker-api 델타: 상/하한가야말로
    감시가 가장 중요한 순간인데 제외하면 결과가 사라짐)."""
    _mock_auth()
    upper_limit = dict(QUOTE_ROW, stk_cd="111111", sel_bid="")   # 상한가: ask 소진
    dead = dict(QUOTE_ROW, stk_cd="222222", cur_prc="")          # 참 degenerate
    respx.post(f"{BASE}/api/dostk/stkinfo").respond(
        json={"return_code": 0, "atn_stk_infr": [QUOTE_ROW, upper_limit, dead]})
    result = await _broker().get_quotes(["005930", "111111", "222222"])
    symbols = [m.quote.symbol for m in result]
    assert symbols == ["005930", "111111"]  # dead만 제외, 편측 호가는 유지
    assert result[1].ask == 0 and result[1].bid > 0


@pytest.mark.anyio
@respx.mock
async def test_잘못된_심볼은_HTTP_호출_전_차단():
    """자금 이동 경로 심볼 형식 fail-loud(보안 패널) — 파이프 스머글링·오발주
    방어. HTTP 스텁을 등록하지 않아 호출이 나가면 테스트가 실패한다."""
    _mock_auth()
    broker = _broker()
    with pytest.raises(ValueError, match="symbol"):
        await broker.get_quotes(["005930", "0059|30"])  # 파이프 주입 시도
    with pytest.raises(ValueError, match="symbol"):
        await broker.cancel_order("0034447", "")  # 빈 심볼
    with pytest.raises(ValueError, match="symbol"):
        OrderRequest(symbol="한글코드", side=OrderSide.BUY,
                     style=OrderStyle.MARKET, quantity=1)  # 비ASCII


@pytest.mark.anyio
@respx.mock
async def test_알수없는_io_tp_nm은_fail_loud():
    _mock_auth()
    respx.post(f"{BASE}/api/dostk/acnt").respond(
        json={"return_code": 0, "oso": [
            {"ord_no": "1", "stk_cd": "005930", "io_tp_nm": "정정",
             "ord_qty": "1", "oso_qty": "1", "ord_pric": "1000", "ord_stt": "접수"}]})
    with pytest.raises(BrokerError, match="ka10075"):
        await _broker().get_open_orders()
