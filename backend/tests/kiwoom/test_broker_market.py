import json
from datetime import date
from decimal import Decimal

import pytest
import respx

from app.adapters.kiwoom.broker import KiwoomBroker
from app.adapters.kiwoom.client import KiwoomHttpClient
from app.core.config import Settings
from app.domain.broker import BrokerPort

BASE = "https://mockapi.kiwoom.com"
TOKEN_JSON = {"token": "TOK", "token_type": "bearer",
              "expires_dt": "20991231235959", "return_code": 0, "return_msg": "ok"}

# ⚠️ 아래 픽스처 필드명은 비공식 리서치 기반 — 라이브 스모크 실측 후 필요 시 수정
QUOTE_JSON = {"return_code": 0, "return_msg": "ok", "stk_cd": "005930",
              "stk_nm": "삼성전자", "cur_prc": "+71000", "flu_rt": "+1.25",
              "trde_qty": "12345678"}

CANDLE_PAGE = {"return_code": 0, "stk_cd": "005930", "stk_dt_pole_chart_qry": [
    {"dt": "20260717", "open_pric": "70500", "high_pric": "71200",
     "low_pric": "70100", "cur_prc": "71000", "trde_qty": "111"},
    {"dt": "20260716", "open_pric": "70000", "high_pric": "70800",
     "low_pric": "69900", "cur_prc": "70500", "trde_qty": "222"},
    {"dt": "20260715", "open_pric": "69500", "high_pric": "70100",
     "low_pric": "69400", "cur_prc": "70000", "trde_qty": "333"},
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
async def test_get_quote는_도메인_모델로_변환한다():
    _mock_auth()
    respx.post(f"{BASE}/api/dostk/stkinfo").respond(
        json=QUOTE_JSON, headers={"cont-yn": "N", "next-key": ""})
    b = _broker()
    q = await b.get_quote("005930")
    assert q.symbol == "005930" and q.name == "삼성전자"
    assert q.price == 71000          # 부호 제거
    assert q.change_rate == Decimal("1.25")
    assert q.volume == 12_345_678
    await b.aclose()


@pytest.mark.anyio
@respx.mock
async def test_get_quote는_flu_rt가_공백이면_0으로_처리한다():
    _mock_auth()
    blank_quote = {**QUOTE_JSON, "flu_rt": "  "}
    respx.post(f"{BASE}/api/dostk/stkinfo").respond(
        json=blank_quote, headers={"cont-yn": "N", "next-key": ""})
    b = _broker()
    q = await b.get_quote("005930")
    assert q.change_rate == Decimal("0")
    await b.aclose()


@pytest.mark.anyio
@respx.mock
async def test_get_daily_candles는_과거_최신_순으로_count개를_반환한다():
    _mock_auth()
    respx.post(f"{BASE}/api/dostk/chart").respond(
        json=CANDLE_PAGE, headers={"cont-yn": "N", "next-key": ""})
    b = _broker()
    candles = await b.get_daily_candles("005930", count=2)
    assert len(candles) == 2
    assert candles[0].date == date(2026, 7, 16)   # 과거가 먼저
    assert candles[1].date == date(2026, 7, 17)
    assert candles[1].close == 71000 and candles[1].volume == 111
    await b.aclose()


@pytest.mark.anyio
@respx.mock
async def test_get_daily_candles는_주입된_today를_base_dt로_사용한다():
    _mock_auth()
    route = respx.post(f"{BASE}/api/dostk/chart").respond(
        json=CANDLE_PAGE, headers={"cont-yn": "N", "next-key": ""})
    s = Settings(_env_file=None, kiwoom_app_key="AK", kiwoom_secret_key="SK",
                 kiwoom_mock=True, database_url="sqlite+pysqlite:///:memory:")
    b = KiwoomBroker(KiwoomHttpClient(s, sleep=_noop_sleep), today=lambda: date(2026, 7, 10))
    await b.get_daily_candles("005930", count=2)
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body["base_dt"] == "20260710"
    await b.aclose()


def test_KiwoomBroker는_BrokerPort_계약을_만족한다():
    # __new__로 생성해 리소스(httpx client) 없이 클래스 구조만 검사한다
    instance = KiwoomBroker.__new__(KiwoomBroker)
    assert isinstance(instance, BrokerPort) is False  # Task 7 완료 전 — 계좌 메서드 미구현
