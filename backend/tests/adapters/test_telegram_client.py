import logging

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.telegram.client import (
    TelegramAuthenticationError,
    TelegramClient,
    TelegramPermanentError,
    TelegramRateLimited,
    TelegramTemporaryError,
)
from app.core.sensitive_logging import configure_sensitive_http_logging


TOKEN = "TOPSECRET"


def _client(handler) -> TelegramClient:
    return TelegramClient(SecretStr(TOKEN), transport=httpx.MockTransport(handler))


@pytest.mark.anyio
async def test_get_updates는_고정_polling_계약과_dto를_사용한다():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == f"https://api.telegram.org/bot{TOKEN}/getUpdates"
        body = __import__("json").loads(request.content)
        assert body == {
            "offset": 41,
            "timeout": 30,
            "limit": 100,
            "allowed_updates": ["message"],
        }
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 42,
                        "message": {
                            "from": {"id": 10},
                            "chat": {"id": 20, "type": "private"},
                            "text": "/status",
                            "forward_origin": {"type": "user"},
                        },
                    },
                    {
                        "update_id": 43,
                        "message": {
                            "from": {"id": 10},
                            "chat": {"id": 20, "type": "private"},
                        },
                    },
                ],
            },
        )

    async with _client(handler) as client:
        messages = await client.get_updates(41)

    assert messages[0].update_id == 42
    assert messages[0].text == "/status"
    assert messages[0].forwarded is True
    assert messages[1].text == ""
    assert messages[1].forwarded is False


@pytest.mark.anyio
async def test_send_message는_plain_text만_전송한다():
    async def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        assert body == {"chat_id": 20, "text": "<b>plain</b>"}
        assert "parse_mode" not in body
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    client = _client(handler)
    message_id = await client.send_message(20, "<b>plain</b>")
    await client.aclose()
    assert message_id == 77


@pytest.mark.anyio
@pytest.mark.parametrize("result", [{}, {"message_id": "77"}])
async def test_send_message는_정확한_message_id를_요구한다(result):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": result})

    async with _client(handler) as client:
        with pytest.raises(TelegramPermanentError, match="invalid_response"):
            await client.send_message(20, "hello")


@pytest.mark.anyio
async def test_미지원_update가_정상_긴급명령_batch를_막지_않는다():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {"update_id": 50, "my_chat_member": {"new_chat_member": {}}},
                    {
                        "update_id": 51,
                        "message": {
                            "from": {"id": 10},
                            "chat": {"id": 20, "type": "private"},
                            "text": "/stop",
                        },
                    },
                ],
            },
        )

    async with _client(handler) as client:
        messages = await client.get_updates(50)

    assert [(item.update_id, item.text) for item in messages] == [(50, ""), (51, "/stop")]
    assert messages[0].user_id == 0
    assert messages[0].chat_type == "unsupported"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "damaged",
    [
        "not-an-object",
        {},
        {"update_id": "52"},
        {"update_id": True},
    ],
)
async def test_손상된_update가_뒤의_stop을_막지_않는다(damaged):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    damaged,
                    {
                        "update_id": 53,
                        "message": {
                            "from": {"id": 10},
                            "chat": {"id": 20, "type": "private"},
                            "text": "/stop",
                        },
                    },
                ],
            },
        )

    async with _client(handler) as client:
        messages = await client.get_updates(50)

    assert [(item.update_id, item.text) for item in messages] == [(53, "/stop")]


@pytest.mark.anyio
async def test_정상_update_뒤의_더큰_malformed_ID는_offset근거로_쓰지_않는다():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 53,
                        "message": {
                            "from": {"id": 10},
                            "chat": {"id": 20, "type": "private"},
                            "text": "/stop",
                        },
                    },
                    {"update_id": "999", "message": {}},
                ],
            },
        )

    async with _client(handler) as client:
        messages = await client.get_updates(50)

    assert [item.update_id for item in messages] == [53]


@pytest.mark.anyio
async def test_401과_403은_공유_auth_circuit를_열고_추가호출을_막는다():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"ok": False})

    async with _client(handler) as client:
        with pytest.raises(TelegramAuthenticationError):
            await client.get_updates(0)
        with pytest.raises(TelegramAuthenticationError):
            await client.send_message(20, "hello")
    assert calls == 1


@pytest.mark.anyio
async def test_비JSON_403도_auth_circuit를_연다():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, text="<html>forbidden</html>")

    async with _client(handler) as client:
        with pytest.raises(TelegramAuthenticationError):
            await client.get_updates(0)
        with pytest.raises(TelegramAuthenticationError):
            await client.send_message(20, "hello")
    assert calls == 1


@pytest.mark.anyio
async def test_429는_retry_after를_보존한다():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"ok": False, "parameters": {"retry_after": 17}},
        )

    async with _client(handler) as client:
        with pytest.raises(TelegramRateLimited) as caught:
            await client.send_message(20, "hello")
    assert caught.value.retry_after == 17


@pytest.mark.anyio
async def test_비JSON_HTTP_429는_기본_retry_after를_사용한다():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    async with _client(handler) as client:
        with pytest.raises(TelegramRateLimited) as caught:
            await client.get_updates(0)
    assert caught.value.retry_after == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [(302, TelegramPermanentError), (400, TelegramPermanentError),
     (500, TelegramTemporaryError)],
)
async def test_http_status를_분류한다(status, error_type):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"ok": False})

    async with _client(handler) as client:
        with pytest.raises(error_type) as caught:
            await client.send_message(20, "hello")
    assert caught.value.endpoint == "sendMessage"
    assert TOKEN not in str(caught.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "error_type", "kind"),
    [
        (408, TelegramTemporaryError, "timeout"),
        (500, TelegramTemporaryError, "server"),
    ],
)
async def test_HTTP_decision_table은_body형식에_의존하지_않는다(
    status, error_type, kind
):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=[])

    async with _client(handler) as client:
        with pytest.raises(error_type) as caught:
            await client.get_updates(0)
    assert caught.value.kind == kind


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error_code", "error_type", "kind"),
    [
        (401, TelegramAuthenticationError, "authentication"),
        (429, TelegramRateLimited, "rate_limited"),
        (408, TelegramTemporaryError, "timeout"),
        (500, TelegramTemporaryError, "server"),
        (400, TelegramPermanentError, "api_error"),
    ],
)
async def test_2xx_envelope_error_code_decision_table(error_code, error_type, kind):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "error_code": error_code},
        )

    async with _client(handler) as client:
        with pytest.raises(error_type) as caught:
            await client.get_updates(0)
    assert caught.value.kind == kind
    if error_code == 429:
        assert caught.value.retry_after == 1


@pytest.mark.anyio
@pytest.mark.parametrize("error_code", [True, "500", 500.0])
async def test_malformed_error_code는_invalid_response다(error_code):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "error_code": error_code},
        )

    async with _client(handler) as client:
        with pytest.raises(TelegramPermanentError) as caught:
            await client.get_updates(0)
    assert caught.value.kind == "invalid_response"


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["network", "json", "ok_false"])
async def test_transport_json과_telegram_오류를_안전하게_분류한다(failure):
    async def handler(request: httpx.Request) -> httpx.Response:
        if failure == "network":
            raise httpx.ConnectError(f"failed {request.url}", request=request)
        if failure == "json":
            return httpx.Response(200, text="not-json")
        return httpx.Response(200, json={"ok": False, "description": TOKEN})

    expected = TelegramTemporaryError if failure == "network" else TelegramPermanentError
    async with _client(handler) as client:
        with pytest.raises(expected) as caught:
            await client.get_updates(0)
    assert caught.value.endpoint == "getUpdates"
    assert TOKEN not in str(caught.value)
    assert TOKEN not in repr(caught.value)
    if failure == "json":
        assert caught.value.__cause__ is None


@pytest.mark.anyio
async def test_redirect와_네트워크_예외가_token이나_url을_로그하지_않는다(caplog):
    caplog.set_level(logging.DEBUG)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example"})

    async with _client(handler) as client:
        with pytest.raises(TelegramPermanentError):
            await client.send_message(20, "hello")

    assert TOKEN not in caplog.text
    assert f"/bot{TOKEN}/" not in caplog.text
    assert "https://api.telegram.org" not in caplog.text


def test_sensitive_HTTP_access_log는_child_namespace에서도_차단된다(caplog):
    configure_sensitive_http_logging()
    caplog.set_level(logging.DEBUG)

    logging.getLogger("httpx").info("POST https://api.telegram.org/bot%s/getUpdates", TOKEN)
    logging.getLogger("httpcore.http11").debug(
        "send_request_headers path=/bot%s/sendMessage", TOKEN
    )
    logging.getLogger("httpcore.http11").warning("connection pool unavailable")

    assert TOKEN not in caplog.text
    assert "api.telegram.org" not in caplog.text
    assert "connection pool unavailable" in caplog.text


@pytest.mark.anyio
async def test_client_수명과_중앙_logging_설정은_결합되지_않는다():
    before = {
        name: tuple(logging.getLogger(name).filters)
        for name in ("httpx", "httpcore")
    }

    first = _client(lambda request: httpx.Response(200, json={"ok": True, "result": []}))
    second = _client(lambda request: httpx.Response(200, json={"ok": True, "result": []}))
    await first.aclose()
    await second.aclose()

    assert {
        name: tuple(logging.getLogger(name).filters)
        for name in ("httpx", "httpcore")
    } == before
