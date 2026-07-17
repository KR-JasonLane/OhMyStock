"""키움 OAuth2 토큰 수명주기. 토큰은 메모리에만 보관하고 로그에 남기지 않는다."""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.adapters.kiwoom.errors import AuthError, RateLimitError

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
_EXPIRES_FMT = "%Y%m%d%H%M%S"  # 키움 expires_dt: 절대 만료시각(KST)


class TokenManager:
    def __init__(
        self,
        http: httpx.AsyncClient,
        app_key: str,
        secret_key: str,
        margin_seconds: int = 60,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._http = http
        self._app_key = app_key
        self._secret_key = secret_key
        self._margin = timedelta(seconds=margin_seconds)
        self._now = now or (lambda: datetime.now(KST))
        self._token: str | None = None
        self._expires_at: datetime | None = None
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        async with self._lock:
            if self._token is None or self._is_expiring():
                await self._issue()
            assert self._token is not None
            return self._token

    def invalidate(self) -> None:
        """서버가 토큰을 거부했을 때(만료 등) 캐시를 버려 다음 호출에서 재발급되게 한다."""
        self._token = None
        self._expires_at = None

    async def revoke(self) -> None:
        async with self._lock:  # get_token()/_issue()와 경합해 방금 발급한 토큰을 지우지 않도록
            if self._token is None:
                return
            try:
                resp = await self._http.post(
                    "/oauth2/revoke",
                    json={"appkey": self._app_key, "secretkey": self._secret_key,
                          "token": self._token},
                )
                try:
                    code = resp.json().get("return_code")
                except ValueError:
                    code = None
                if code == 0:
                    logger.info("kiwoom token revoked")
                else:
                    logger.warning("kiwoom token revoke returned code=%s", code)
            except httpx.HTTPError as exc:  # 종료 경로 — 실패해도 앱 종료를 막지 않는다
                logger.warning("kiwoom token revoke failed: %s", type(exc).__name__)
            finally:
                self.invalidate()

    def _is_expiring(self) -> bool:
        return self._expires_at is None or self._now() >= self._expires_at - self._margin

    async def _issue(self) -> None:
        try:
            resp = await self._http.post(
                "/oauth2/token",
                json={"grant_type": "client_credentials",
                      "appkey": self._app_key, "secretkey": self._secret_key},
            )
        except httpx.HTTPError as exc:
            raise AuthError(f"token issue failed: network {type(exc).__name__}") from exc

        if resp.status_code == 429:
            raise RateLimitError("token issue rate limited")

        try:
            data = resp.json()
        except ValueError:
            raise AuthError(
                f"token issue failed: non-json response http={resp.status_code}"
            ) from None

        if resp.status_code != 200 or data.get("return_code") != 0 or not data.get("token"):
            # 시크릿/토큰은 메시지에 넣지 않는다
            raise AuthError(
                f"token issue failed: http={resp.status_code} "
                f"code={data.get('return_code')} msg={data.get('return_msg')}"
            )
        self._token = data["token"]
        self._expires_at = datetime.strptime(
            data["expires_dt"], _EXPIRES_FMT).replace(tzinfo=KST)
        logger.info("kiwoom token issued, expires_at=%s", self._expires_at.isoformat())
