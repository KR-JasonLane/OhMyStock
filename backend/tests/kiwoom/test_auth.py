from datetime import datetime

import httpx
import pytest
import respx

from app.adapters.kiwoom.auth import KST, TokenManager
from app.adapters.kiwoom.errors import AuthError, RateLimitError

BASE = "https://mockapi.kiwoom.com"


def _token_response(token: str, expires_dt: str, code: int = 0) -> dict:
    return {"token": token, "token_type": "bearer", "expires_dt": expires_dt,
            "return_code": code, "return_msg": "ok"}


def _manager(now: datetime) -> tuple[TokenManager, httpx.AsyncClient]:
    http = httpx.AsyncClient(base_url=BASE)
    tm = TokenManager(http, app_key="AK", secret_key="SK", now=lambda: now)
    return tm, http


@pytest.mark.anyio
@respx.mock
async def test_최초_호출시_토큰을_발급한다():
    now = datetime(2026, 7, 17, 9, 0, 0, tzinfo=KST)
    route = respx.post(f"{BASE}/oauth2/token").respond(
        json=_token_response("TOK1", "20260717235959"))
    tm, http = _manager(now)
    assert await tm.get_token() == "TOK1"
    assert route.call_count == 1
    body = route.calls[0].request.content
    assert b"client_credentials" in body and b"AK" in body
    await http.aclose()


@pytest.mark.anyio
@respx.mock
async def test_만료_전에는_캐시를_재사용한다():
    now = datetime(2026, 7, 17, 9, 0, 0, tzinfo=KST)
    route = respx.post(f"{BASE}/oauth2/token").respond(
        json=_token_response("TOK1", "20260717235959"))
    tm, http = _manager(now)
    await tm.get_token()
    await tm.get_token()
    assert route.call_count == 1
    await http.aclose()


@pytest.mark.anyio
@respx.mock
async def test_만료_임박시_재발급한다():
    # 만료 09:00:30, 마진 60초 → 09:00:00 시점엔 이미 임박 → 두 번째 호출도 재발급
    now = datetime(2026, 7, 17, 9, 0, 0, tzinfo=KST)
    route = respx.post(f"{BASE}/oauth2/token").respond(
        json=_token_response("TOK", "20260717090030"))
    tm, http = _manager(now)
    await tm.get_token()
    await tm.get_token()
    assert route.call_count == 2
    await http.aclose()


@pytest.mark.anyio
@respx.mock
async def test_발급_실패시_AuthError():
    now = datetime(2026, 7, 17, 9, 0, 0, tzinfo=KST)
    respx.post(f"{BASE}/oauth2/token").respond(
        json=_token_response("", "20260717235959", code=8005))
    tm, http = _manager(now)
    with pytest.raises(AuthError):
        await tm.get_token()
    await http.aclose()


@pytest.mark.anyio
@respx.mock
async def test_네트워크_오류시_AuthError():
    now = datetime(2026, 7, 17, 9, 0, 0, tzinfo=KST)
    respx.post(f"{BASE}/oauth2/token").mock(side_effect=httpx.ConnectError("boom"))
    tm, http = _manager(now)
    with pytest.raises(AuthError):
        await tm.get_token()
    await http.aclose()


@pytest.mark.anyio
@respx.mock
async def test_429는_RateLimitError():
    now = datetime(2026, 7, 17, 9, 0, 0, tzinfo=KST)
    respx.post(f"{BASE}/oauth2/token").respond(status_code=429)
    tm, http = _manager(now)
    with pytest.raises(RateLimitError):
        await tm.get_token()
    await http.aclose()


@pytest.mark.anyio
@respx.mock
async def test_비JSON_응답시_AuthError():
    now = datetime(2026, 7, 17, 9, 0, 0, tzinfo=KST)
    respx.post(f"{BASE}/oauth2/token").respond(
        content=b"<html>", headers={"content-type": "text/html"})
    tm, http = _manager(now)
    with pytest.raises(AuthError):
        await tm.get_token()
    await http.aclose()


@pytest.mark.anyio
@respx.mock
async def test_invalidate_후에는_재발급한다():
    now = datetime(2026, 7, 17, 9, 0, 0, tzinfo=KST)
    route = respx.post(f"{BASE}/oauth2/token").respond(
        json=_token_response("TOK1", "20260717235959"))
    tm, http = _manager(now)
    await tm.get_token()
    tm.invalidate()
    await tm.get_token()
    assert route.call_count == 2
    await http.aclose()


@pytest.mark.anyio
@respx.mock
async def test_revoke는_서버에_폐기를_요청하고_캐시를_비운다():
    now = datetime(2026, 7, 17, 9, 0, 0, tzinfo=KST)
    token_route = respx.post(f"{BASE}/oauth2/token").respond(
        json=_token_response("TOK1", "20260717235959"))
    revoke_route = respx.post(f"{BASE}/oauth2/revoke").respond(
        json={"return_code": 0, "return_msg": "ok"})
    tm, http = _manager(now)
    await tm.get_token()
    await tm.revoke()
    assert revoke_route.call_count == 1
    await tm.get_token()                 # 캐시가 비워졌으므로 재발급
    assert token_route.call_count == 2
    await http.aclose()
