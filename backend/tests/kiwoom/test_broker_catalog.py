import json

import pytest
import respx

from app.adapters.kiwoom.broker import KiwoomBroker
from app.adapters.kiwoom.client import KiwoomHttpClient
from app.core.config import Settings
from app.domain.errors import BrokerError

BASE = "https://mockapi.kiwoom.com"
TOKEN_JSON = {"token": "TOK", "token_type": "bearer",
              "expires_dt": "20991231235959", "return_code": 0, "return_msg": "ok"}

# 스파이크 실측 필드명 (2026-07-17, 모의서버) — ka10099: list, code/name/marketCode/
# marketName/upName/... , instrument_type은 kind 필드 원문.
INSTRUMENTS_JSON = {"return_code": 0, "list": [
    {"code": "005930", "name": "삼성전자", "marketCode": "0", "marketName": "거래소",
     "upName": "전기전자", "kind": "0"},
    {"code": "000660", "name": "SK하이닉스", "marketCode": "0", "marketName": "거래소",
     "upName": "전기전자", "kind": "0"},
]}

# ka10101: list, marketCode/code/name/group. 시장코드는 ka10099와 달리 코스닥=1.
SECTORS_JSON = {"return_code": 0, "list": [
    {"marketCode": "0", "code": "001", "name": "종합(KOSPI)", "group": "0"},
    {"marketCode": "0", "code": "013", "name": "전기전자", "group": "0"},
]}
SECTORS_KOSDAQ_JSON = {"return_code": 0, "list": [
    {"marketCode": "1", "code": "101", "name": "종합(KOSDAQ)", "group": "0"},
]}

# ka20002: inds_stkpc, stk_cd(6자리, 접두 없음)/stk_nm/가격 필드.
MEMBERS_JSON = {"return_code": 0, "inds_stkpc": [
    {"stk_cd": "005930", "stk_nm": "삼성전자"},
    {"stk_cd": "000660", "stk_nm": "SK하이닉스"},
]}


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
async def test_list_instruments는_도메인_모델로_변환한다():
    _mock_auth()
    route = respx.post(f"{BASE}/api/dostk/stkinfo").respond(
        json=INSTRUMENTS_JSON, headers={"cont-yn": "N", "next-key": ""})
    b = _broker()
    items = await b.list_instruments("kospi")
    assert [i.symbol for i in items] == ["005930", "000660"]
    assert items[0].market == "kospi" and items[0].name == "삼성전자"
    assert items[0].instrument_type == "0"  # kind 필드 원문
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body["mrkt_tp"] == "0"  # kospi
    await b.aclose()


@pytest.mark.anyio
@respx.mock
async def test_list_instruments는_상태_필드를_매핑한다():
    # 실측: ka10099 행에는 state(증거금/신용 등급 등)와 auditInfo(관리종목 여부
    # 등)가 원문 문자열로 실려 온다 — Instrument.state/audit_info로 그대로 매핑.
    _mock_auth()
    state_json = {"return_code": 0, "list": [
        {"code": "005930", "name": "삼성전자", "marketCode": "0", "marketName": "거래소",
         "upName": "전기전자", "kind": "0",
         "state": "증거금20%|담보대출|신용가능", "auditInfo": "정상"},
    ]}
    respx.post(f"{BASE}/api/dostk/stkinfo").respond(
        json=state_json, headers={"cont-yn": "N", "next-key": ""})
    b = _broker()
    items = await b.list_instruments("kospi")
    assert items[0].state == "증거금20%|담보대출|신용가능"
    assert items[0].audit_info == "정상"
    await b.aclose()


@pytest.mark.anyio
@respx.mock
async def test_list_instruments는_모르는_시장이면_ValueError():
    _mock_auth()
    b = _broker()
    with pytest.raises(ValueError):
        await b.list_instruments("nasdaq")
    await b.aclose()


@pytest.mark.anyio
@respx.mock
async def test_list_instruments는_code_형식이_이상하면_BrokerError():
    _mock_auth()
    bad_json = {"return_code": 0, "list": [
        {"code": "XX123", "name": "이상한종목", "marketCode": "0", "kind": "0"}]}
    respx.post(f"{BASE}/api/dostk/stkinfo").respond(
        json=bad_json, headers={"cont-yn": "N", "next-key": ""})
    b = _broker()
    with pytest.raises(BrokerError):
        await b.list_instruments("kospi")
    await b.aclose()


@pytest.mark.anyio
@respx.mock
async def test_list_instruments는_유니코드_code면_BrokerError():
    # isalnum() 단독 검증은 한글 등 유니코드 문자도 "영숫자"로 통과시킨다 —
    # isascii()를 함께 요구해야 이런 코드가 fail-loud로 걸러진다(회귀 방지).
    _mock_auth()
    bad_json = {"return_code": 0, "list": [
        {"code": "가나다라마바", "name": "유니코드코드", "marketCode": "0", "kind": "0"}]}
    respx.post(f"{BASE}/api/dostk/stkinfo").respond(
        json=bad_json, headers={"cont-yn": "N", "next-key": ""})
    b = _broker()
    with pytest.raises(BrokerError):
        await b.list_instruments("kospi")
    await b.aclose()


@pytest.mark.anyio
@respx.mock
async def test_list_instruments는_다른_marketCode_행을_제외한다():
    # 실측: mrkt_tp="0"(kospi) 요청 응답에도 ETF(marketCode="8") 등 다른
    # marketCode를 가진 행이 섞여 온다 — 걸러지지 않으면 ETF가
    # market="kospi"인 Instrument로 오라벨된다.
    _mock_auth()
    mixed_json = {"return_code": 0, "list": [
        {"code": "005930", "name": "삼성전자", "marketCode": "0",
         "marketName": "거래소", "kind": "0"},
        {"code": "0000D0", "name": "TIGER미국채", "marketCode": "8",
         "marketName": "ETF", "kind": "0"},
    ]}
    respx.post(f"{BASE}/api/dostk/stkinfo").respond(
        json=mixed_json, headers={"cont-yn": "N", "next-key": ""})
    b = _broker()
    items = await b.list_instruments("kospi")
    assert [i.symbol for i in items] == ["005930"]  # ETF 행(marketCode=8)은 제외
    await b.aclose()


@pytest.mark.anyio
@respx.mock
async def test_list_sectors와_members():
    _mock_auth()
    # kospi(mrkt_tp=0)와 kosdaq(mrkt_tp=1) 요청에 서로 다른 fixture를 응답시켜
    # (ka10099와 코스닥 값이 다르다는 문서화된 실수 유발 포인트를) 실제로 각
    # 호출에 올바른 mrkt_tp가 실렸는지 검증한다 — 값이 뒤바뀌면(회귀) 아래
    # sectors[1]/sent_body 단언이 실패한다.
    def _route_by_mrkt_tp(request):
        import httpx
        body = json.loads(request.content)
        payload = SECTORS_JSON if body["mrkt_tp"] == "0" else SECTORS_KOSDAQ_JSON
        return httpx.Response(200, json=payload,
                               headers={"cont-yn": "N", "next-key": ""})

    respx.post(f"{BASE}/api/dostk/stkinfo").mock(side_effect=_route_by_mrkt_tp)
    members_route = respx.post(f"{BASE}/api/dostk/sect").respond(
        json=MEMBERS_JSON, headers={"cont-yn": "N", "next-key": ""})
    b = _broker()
    sectors = await b.list_sectors()
    assert [s.code for s in sectors] == ["001", "013", "101"]
    assert sectors[1].name == "전기전자" and sectors[1].market == "kospi"
    assert sectors[2].name == "종합(KOSDAQ)" and sectors[2].market == "kosdaq"

    members = await b.list_sector_members("013", "kospi")
    assert members == ["005930", "000660"]

    sent_body = json.loads(members_route.calls.last.request.content)
    assert sent_body == {"mrkt_tp": "0", "inds_cd": "013", "stex_tp": "1"}
    await b.aclose()


@pytest.mark.anyio
@respx.mock
async def test_list_sectors는_code_필드_누락시_BrokerError():
    _mock_auth()
    bad_json = {"return_code": 0, "list": [{"name": "이름만있음", "marketCode": "0"}]}
    respx.post(f"{BASE}/api/dostk/stkinfo").respond(
        json=bad_json, headers={"cont-yn": "N", "next-key": ""})
    b = _broker()
    with pytest.raises(BrokerError):
        await b.list_sectors()
    await b.aclose()


@pytest.mark.anyio
@respx.mock
async def test_list_sector_members는_모르는_시장이면_ValueError():
    _mock_auth()
    b = _broker()
    with pytest.raises(ValueError):
        await b.list_sector_members("013", "nyse")
    await b.aclose()


@pytest.mark.anyio
@respx.mock
async def test_list_sector_members는_stk_cd_형식이_이상하면_BrokerError():
    _mock_auth()
    bad_json = {"return_code": 0, "inds_stkpc": [{"stk_cd": "XX123", "stk_nm": "이상"}]}
    respx.post(f"{BASE}/api/dostk/sect").respond(
        json=bad_json, headers={"cont-yn": "N", "next-key": ""})
    b = _broker()
    with pytest.raises(BrokerError):
        await b.list_sector_members("013", "kospi")
    await b.aclose()
