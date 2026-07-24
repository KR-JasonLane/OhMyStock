"""고정된 공식 Bot API 경계.

token이 Telegram URL path에 들어가므로 모든 외부 오류를 endpoint label만
가진 예외로 변환한다. 생성한 HTTP client는 ``aclose`` 또는 async context
manager로 반드시 닫는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import SecretStr

from app.core.sensitive_logging import configure_sensitive_http_logging
from app.domain.notifications.models import InboundMessage

# 앱 진입점 밖에서 client를 직접 사용하는 worker/test도 token URL access log를
# 켜지 못하게 하는 fail-safe다. 함수는 namespace level만 멱등 설정한다.
configure_sensitive_http_logging()


class TelegramError(RuntimeError):
    def __init__(self, endpoint: str, kind: str) -> None:
        super().__init__(f"Telegram {endpoint} failed ({kind})")
        self.endpoint = endpoint
        self.kind = kind


class TelegramPermanentError(TelegramError):
    """재시도해도 같은 요청이 성공하지 않는 응답/프로토콜 오류."""


class TelegramTemporaryError(TelegramError):
    """네트워크 및 서버 장애처럼 backoff 후 재시도할 수 있는 오류."""


class TelegramAuthenticationError(TelegramPermanentError):
    def __init__(self, endpoint: str) -> None:
        super().__init__(endpoint, "authentication")


class TelegramRateLimited(TelegramTemporaryError):
    def __init__(self, endpoint: str, retry_after: int) -> None:
        super().__init__(endpoint, "rate_limited")
        self.retry_after = retry_after


class TelegramClient:
    _ORIGIN = "https://api.telegram.org"

    def __init__(
        self,
        token: SecretStr,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._authentication_failed = False
        self._http = httpx.AsyncClient(
            verify=True,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(35.0),
            transport=transport,
        )

    def _endpoint_url(self, endpoint: str) -> str:
        token = self._token.get_secret_value()
        return f"{self._ORIGIN}/bot{token}/{endpoint}"

    async def get_updates(self, offset: int) -> list[InboundMessage]:
        data = await self._post(
            "getUpdates",
            {
                "offset": offset,
                "timeout": 30,
                "limit": 100,
                "allowed_updates": ["message"],
            },
        )
        result = data.get("result")
        if not isinstance(result, list):
            raise TelegramPermanentError("getUpdates", "invalid_response")
        normalized: list[InboundMessage] = []
        for update in result:
            message = _to_inbound_or_none(update)
            if message is not None:
                normalized.append(message)
        return normalized

    async def send_message(self, chat_id: int, text: str) -> int:
        data = await self._post("sendMessage", {"chat_id": chat_id, "text": text})
        result = data.get("result")
        if not isinstance(result, Mapping):
            raise TelegramPermanentError("sendMessage", "invalid_response")
        try:
            return _exact_int(result["message_id"])
        except (KeyError, TypeError):
            raise TelegramPermanentError("sendMessage", "invalid_response") from None

    async def _post(
        self, endpoint: str, payload: Mapping[str, object]
    ) -> Mapping[str, Any]:
        if self._authentication_failed:
            raise TelegramAuthenticationError(endpoint)
        try:
            response = await self._http.post(
                self._endpoint_url(endpoint),
                json=payload,
            )
        except httpx.HTTPError:
            # httpx 예외는 request URL(token 포함)을 보존할 수 있으므로 cause를
            # 연결하지 않는다. endpoint label만 호출자 경계로 내보낸다.
            raise TelegramTemporaryError(endpoint, "network") from None

        status = response.status_code
        if status in (401, 403):
            self._authentication_failed = True
            raise TelegramAuthenticationError(endpoint)
        if status == 429:
            raise TelegramRateLimited(endpoint, _retry_after_from_response(response))
        if status == 408:
            raise TelegramTemporaryError(endpoint, "timeout")
        if status >= 500:
            raise TelegramTemporaryError(endpoint, "server")
        if 300 <= status < 500:
            raise TelegramPermanentError(endpoint, f"http_{status}")
        try:
            data = response.json()
        except ValueError:
            raise TelegramPermanentError(endpoint, "invalid_json") from None
        if not isinstance(data, Mapping):
            raise TelegramPermanentError(endpoint, "invalid_response")

        error_code = data.get("error_code")
        if "error_code" in data and type(error_code) is not int:
            raise TelegramPermanentError(endpoint, "invalid_response")
        if type(error_code) is int:
            if error_code in (401, 403):
                self._authentication_failed = True
                raise TelegramAuthenticationError(endpoint)
            if error_code == 429:
                raise TelegramRateLimited(endpoint, _retry_after_from_mapping(data))
            if error_code == 408:
                raise TelegramTemporaryError(endpoint, "timeout")
            if error_code >= 500:
                raise TelegramTemporaryError(endpoint, "server")
            if 400 <= error_code < 500:
                raise TelegramPermanentError(endpoint, "api_error")
        if data.get("ok") is not True:
            raise TelegramPermanentError(endpoint, "api_error")
        return data

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> TelegramClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


def _to_inbound_or_none(update: object) -> InboundMessage | None:
    if not isinstance(update, Mapping):
        return None
    try:
        update_id = _exact_int(update["update_id"])
    except (KeyError, TypeError):
        return None
    try:
        message = update["message"]
        if not isinstance(message, Mapping):
            raise TypeError("message must be an object")
        sender = message["from"]
        chat = message["chat"]
        if not isinstance(sender, Mapping) or not isinstance(chat, Mapping):
            raise TypeError("sender and chat must be objects")
        text = message.get("text", "")
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        return InboundMessage(
            update_id=update_id,
            user_id=_exact_int(sender["id"]),
            chat_id=_exact_int(chat["id"]),
            chat_type=_exact_str(chat["type"]),
            text=text,
            forwarded=("forward_origin" in message or "forward_date" in message),
        )
    except (KeyError, TypeError, ValueError):
        # allowed_updates 변경 전에 쌓인 비-message update나 불완전한 message
        # 하나가 batch의 정상 /stop을 막지 않게 한다. update_id는 유지해
        # poller가 같은 트랜잭션에서 미허용 집계 후 offset을 전진할 수 있고,
        # 0/unsupported identity는 인증 경계를 항상 통과하지 못한다.
        return InboundMessage(
            update_id=update_id,
            user_id=0,
            chat_id=0,
            chat_type="unsupported",
            text="",
            forwarded=False,
        )


def _exact_int(value: object) -> int:
    if type(value) is not int:
        raise TypeError("expected int")
    return value


def _exact_str(value: object) -> str:
    if type(value) is not str:
        raise TypeError("expected str")
    return value


def _retry_after_from_response(response: httpx.Response) -> int:
    try:
        data = response.json()
    except ValueError:
        return 1
    return _retry_after_from_mapping(data) if isinstance(data, Mapping) else 1


def _retry_after_from_mapping(data: Mapping[str, object]) -> int:
    parameters = data.get("parameters")
    retry_after = (
        parameters.get("retry_after")
        if isinstance(parameters, Mapping)
        else None
    )
    if type(retry_after) is not int or retry_after < 0:
        return 1
    return retry_after
