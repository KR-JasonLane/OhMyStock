import pytest
import respx

from app.adapters.kiwoom.broker import KiwoomBroker
from app.adapters.kiwoom.client import KiwoomHttpClient
from app.domain.errors import BrokerError
from app.core.config import Settings

BASE = "https://mockapi.kiwoom.com"
TOKEN_JSON = {"token": "TOK", "token_type": "bearer",
              "expires_dt": "20991231235959", "return_code": 0, "return_msg": "ok"}

# ⚠️ 비공식 필드명 — 라이브 실측 후 필요 시 수정
DEPOSIT_JSON = {"return_code": 0, "entr": "000001000000", "ord_alow_amt": "000000900000"}
BALANCE_JSON = {"return_code": 0, "tot_evlt_amt": "000000710000",
                "tot_evlt_pl": "-000000020000",
                "acnt_evlt_remn_indv_tot": [
                    {"stk_cd": "A005930", "stk_nm": "삼성전자", "rmnd_qty": "10",
                     "pur_pric": "69000", "cur_prc": "+71000",
                     "evlt_amt": "000000710000"}]}


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
async def test_get_deposit는_정수_원단위로_변환한다():
    _mock_auth()
    respx.post(f"{BASE}/api/dostk/acnt").respond(
        json=DEPOSIT_JSON, headers={"cont-yn": "N", "next-key": ""})
    b = _broker()
    d = await b.get_deposit()
    assert d.total == 1_000_000 and d.available == 900_000
    await b.aclose()


@pytest.mark.anyio
@respx.mock
async def test_get_balance는_포지션과_손익을_변환한다():
    _mock_auth()
    respx.post(f"{BASE}/api/dostk/acnt").respond(
        json=BALANCE_JSON, headers={"cont-yn": "N", "next-key": ""})
    b = _broker()
    bal = await b.get_balance()
    assert bal.total_eval == 710_000
    assert bal.total_profit == -20_000          # 음수 보존
    assert isinstance(bal.positions, tuple)
    p = bal.positions[0]
    assert p.symbol == "005930"                 # 'A' 접두 제거
    assert p.quantity == 10 and p.avg_price == 69_000 and p.current_price == 71_000
    await b.aclose()


@pytest.mark.anyio
@respx.mock
async def test_get_deposit는_entr_필드_누락시_BrokerError():
    _mock_auth()
    respx.post(f"{BASE}/api/dostk/acnt").respond(
        json={"return_code": 0, "ord_alow_amt": "000000900000"},  # entr 누락
        headers={"cont-yn": "N", "next-key": ""})
    b = _broker()
    with pytest.raises(BrokerError):
        await b.get_deposit()
    await b.aclose()


@pytest.mark.anyio
@respx.mock
async def test_get_balance는_stk_cd_형식이_이상하면_BrokerError():
    _mock_auth()
    bad_json = {**BALANCE_JSON, "acnt_evlt_remn_indv_tot": [
        {"stk_cd": "XX123", "stk_nm": "이상한종목", "rmnd_qty": "10",
         "pur_pric": "69000", "cur_prc": "+71000", "evlt_amt": "000000710000"}]}
    respx.post(f"{BASE}/api/dostk/acnt").respond(
        json=bad_json, headers={"cont-yn": "N", "next-key": ""})
    b = _broker()
    with pytest.raises(BrokerError):
        await b.get_balance()
    await b.aclose()
