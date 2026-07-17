import httpx
import pytest
import respx

from app.adapters.kiwoom.client import KiwoomHttpClient
from app.adapters.kiwoom.errors import ApiError, AuthError, BrokerError, RateLimitError
from app.core.config import Settings

BASE = "https://mockapi.kiwoom.com"
TOKEN_JSON = {"token": "TOK", "token_type": "bearer",
              "expires_dt": "20991231235959", "return_code": 0, "return_msg": "ok"}


def _settings() -> Settings:
    return Settings(_env_file=None, kiwoom_app_key="AK", kiwoom_secret_key="SK",
                    kiwoom_mock=True, database_url="sqlite+pysqlite:///:memory:")


async def _noop_sleep(_: float) -> None:
    return None


def _client() -> KiwoomHttpClient:
    return KiwoomHttpClient(_settings(), sleep=_noop_sleep)


def _mock_auth() -> None:
    """토큰 발급 + (aclose 시 호출되는) 폐기 라우트를 함께 모킹한다."""
    respx.post(f"{BASE}/oauth2/token").respond(json=TOKEN_JSON)
    respx.post(f"{BASE}/oauth2/revoke").respond(json={"return_code": 0})


@pytest.mark.anyio
@respx.mock
async def test_call은_헤더와_바디를_구성하고_JSON을_반환한다():
    _mock_auth()
    route = respx.post(f"{BASE}/api/dostk/stkinfo").respond(
        json={"return_code": 0, "return_msg": "ok", "stk_nm": "삼성전자"},
        headers={"cont-yn": "N", "next-key": ""})
    c = _client()
    data, cont, nk = await c.call("stkinfo", "ka10001", {"stk_cd": "005930"})
    assert data["stk_nm"] == "삼성전자" and cont == "N" and nk == ""
    req = route.calls[0].request
    assert req.headers["api-id"] == "ka10001"
    assert req.headers["authorization"] == "Bearer TOK"
    await c.aclose()


@pytest.mark.anyio
@respx.mock
async def test_return_code가_0이_아니면_ApiError():
    _mock_auth()
    respx.post(f"{BASE}/api/dostk/stkinfo").respond(
        json={"return_code": 3, "return_msg": "조회 오류"})
    c = _client()
    with pytest.raises(ApiError) as ei:
        await c.call("stkinfo", "ka10001", {"stk_cd": "005930"})
    assert ei.value.return_code == 3 and ei.value.api_id == "ka10001"
    await c.aclose()


@pytest.mark.anyio
@respx.mock
async def test_401이면_토큰_재발급_후_1회_재시도한다():
    token_route = respx.post(f"{BASE}/oauth2/token").respond(json=TOKEN_JSON)
    respx.post(f"{BASE}/oauth2/revoke").respond(json={"return_code": 0})
    tr = respx.post(f"{BASE}/api/dostk/stkinfo")
    tr.side_effect = [
        httpx.Response(401, json={"return_msg": "token expired"}),
        httpx.Response(200, json={"return_code": 0, "stk_nm": "삼성전자"},
                       headers={"cont-yn": "N", "next-key": ""}),
    ]
    c = _client()
    data, _, _ = await c.call("stkinfo", "ka10001", {"stk_cd": "005930"})
    assert data["stk_nm"] == "삼성전자"
    assert token_route.call_count == 2  # 최초 발급 + 재발급
    await c.aclose()


@pytest.mark.anyio
@respx.mock
async def test_429는_백오프_재시도_후_소진되면_RateLimitError():
    _mock_auth()
    respx.post(f"{BASE}/api/dostk/stkinfo").respond(429)
    c = _client()
    with pytest.raises(RateLimitError):
        await c.call("stkinfo", "ka10001", {"stk_cd": "005930"})
    await c.aclose()


@pytest.mark.anyio
@respx.mock
async def test_call_paged는_cont_yn_Y_동안_반복한다():
    _mock_auth()
    tr = respx.post(f"{BASE}/api/dostk/chart")
    tr.side_effect = [
        httpx.Response(200, json={"return_code": 0, "page": 1},
                       headers={"cont-yn": "Y", "next-key": "K1"}),
        httpx.Response(200, json={"return_code": 0, "page": 2},
                       headers={"cont-yn": "N", "next-key": ""}),
    ]
    c = _client()
    pages = [p async for p in c.call_paged("chart", "ka10081", {"stk_cd": "005930"})]
    assert [p["page"] for p in pages] == [1, 2]
    # 2번째 요청이 이전 응답의 next-key를 실었는지
    assert tr.calls[1].request.headers["cont-yn"] == "Y"
    assert tr.calls[1].request.headers["next-key"] == "K1"
    await c.aclose()


@pytest.mark.anyio
@respx.mock
async def test_401_후_429_시퀀스에서_백오프_예산이_독립적이다():
    """401 재시도는 429 백오프 예산을 소모하지 않는다 — 두 재시도 경로가 서로 독립임을 증명."""
    token_route = respx.post(f"{BASE}/oauth2/token").respond(json=TOKEN_JSON)
    respx.post(f"{BASE}/oauth2/revoke").respond(json={"return_code": 0})
    tr = respx.post(f"{BASE}/api/dostk/stkinfo")
    tr.side_effect = [
        httpx.Response(401, json={"return_msg": "token expired"}),
        httpx.Response(429),
        httpx.Response(200, json={"return_code": 0, "stk_nm": "삼성전자"},
                       headers={"cont-yn": "N", "next-key": ""}),
    ]
    sleeps: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    c = KiwoomHttpClient(_settings(), sleep=_record_sleep)
    data, _, _ = await c.call("stkinfo", "ka10001", {"stk_cd": "005930"})
    assert data["stk_nm"] == "삼성전자"
    assert token_route.call_count == 2  # 최초 발급 + 재발급 (429는 재발급을 유발하지 않음)
    assert sleeps == [1.0]  # 429는 첫 번째 백오프만 소모 — 401 재시도와 예산을 공유하지 않는다
    await c.aclose()


@pytest.mark.anyio
@respx.mock
async def test_401이_재발급_후에도_반복되면_AuthError():
    respx.post(f"{BASE}/oauth2/token").respond(json=TOKEN_JSON)
    respx.post(f"{BASE}/oauth2/revoke").respond(json={"return_code": 0})
    respx.post(f"{BASE}/api/dostk/stkinfo").respond(
        401, json={"return_msg": "token expired"})
    c = _client()
    with pytest.raises(AuthError):
        await c.call("stkinfo", "ka10001", {"stk_cd": "005930"})
    await c.aclose()


@pytest.mark.anyio
@respx.mock
async def test_200_비JSON_응답이면_BrokerError():
    _mock_auth()
    respx.post(f"{BASE}/api/dostk/stkinfo").respond(
        content=b"<html>", headers={"content-type": "text/html"})
    c = _client()
    with pytest.raises(BrokerError):
        await c.call("stkinfo", "ka10001", {"stk_cd": "005930"})
    await c.aclose()


class _FakeLimiter:
    """RateLimiter 대역 — acquire/penalize 호출을 기록만 한다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def acquire(self, tr_id: str) -> None:
        self.calls.append(("acquire", tr_id))

    async def penalize(self, tr_id: str) -> None:
        self.calls.append(("penalize", tr_id))


@pytest.mark.anyio
@respx.mock
async def test_429_수신시_limiter_penalize를_호출한다():
    _mock_auth()
    tr = respx.post(f"{BASE}/api/dostk/stkinfo")
    tr.side_effect = [
        httpx.Response(429),
        httpx.Response(200, json={"return_code": 0, "stk_nm": "삼성전자"},
                       headers={"cont-yn": "N", "next-key": ""}),
    ]
    limiter = _FakeLimiter()
    c = KiwoomHttpClient(_settings(), limiter=limiter, sleep=_noop_sleep)
    await c.call("stkinfo", "ka10001", {"stk_cd": "005930"})
    assert ("penalize", "ka10001") in limiter.calls
    await c.aclose()


@pytest.mark.anyio
@respx.mock
async def test_주입된_http는_aclose가_닫지_않는다():
    _mock_auth()
    http = httpx.AsyncClient(base_url=BASE)
    c = KiwoomHttpClient(_settings(), http=http, sleep=_noop_sleep)
    await c.aclose()
    assert http.is_closed is False
    await http.aclose()


class _FakeTokenManager:
    """TokenManager 대역 — revoke 호출 여부만 기록한다."""

    def __init__(self) -> None:
        self.revoked = False

    async def get_token(self) -> str:
        return "TOK"

    def invalidate(self) -> None:
        pass

    async def revoke(self) -> None:
        self.revoked = True


@pytest.mark.anyio
@respx.mock
async def test_주입된_token_manager는_aclose가_revoke하지_않는다():
    respx.post(f"{BASE}/api/dostk/stkinfo").respond(
        json={"return_code": 0, "stk_nm": "삼성전자"},
        headers={"cont-yn": "N", "next-key": ""})
    tm = _FakeTokenManager()
    c = KiwoomHttpClient(_settings(), token_manager=tm, sleep=_noop_sleep)
    await c.call("stkinfo", "ka10001", {"stk_cd": "005930"})
    await c.aclose()
    assert tm.revoked is False
