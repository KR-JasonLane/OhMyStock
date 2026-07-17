"""키움 REST 호출의 공통 관문: 인증 헤더, TR 헤더, 레이트리밋, 429/401 재시도,
연속조회(cont-yn/next-key) 반복. TR별 의미는 broker.py가 안다.
긴급 TR 우선순위·타임아웃 정책은 Phase 5(트레이딩 엔진)에서 이 관문 위에 얹는다."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx

from app.adapters.kiwoom.auth import BACKOFF_SECONDS, TokenManager
from app.adapters.kiwoom.rate_limiter import RateLimiter
from app.core.config import Settings
from app.domain.errors import ApiError, AuthError, BrokerError, RateLimitError

logger = logging.getLogger(__name__)

MOCK_BASE = "https://mockapi.kiwoom.com"
REAL_BASE = "https://api.kiwoom.com"


class KiwoomHttpClient:
    def __init__(
        self,
        settings: Settings,
        *,
        token_manager: TokenManager | None = None,
        limiter: RateLimiter | None = None,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        base = MOCK_BASE if settings.kiwoom_mock else REAL_BASE
        self._owns_http = http is None
        self._owns_tokens = token_manager is None
        self._http = http or httpx.AsyncClient(base_url=base, timeout=10.0)
        self._limiter = limiter or RateLimiter()
        self._tokens = token_manager or TokenManager(
            self._http,
            settings.kiwoom_app_key.get_secret_value(),
            settings.kiwoom_secret_key.get_secret_value(),
            limiter=self._limiter,
        )
        self._sleep = sleep or asyncio.sleep

    async def call(
        self, category: str, api_id: str, body: dict,
        cont_yn: str = "N", next_key: str = "",
    ) -> tuple[dict, str, str]:
        await self._limiter.acquire(api_id)
        reissued = False
        backoff_idx = 0
        while True:
            headers = {
                "authorization": f"Bearer {await self._tokens.get_token()}",
                "api-id": api_id,
            }
            if cont_yn == "Y":
                headers["cont-yn"] = "Y"
                headers["next-key"] = next_key
            try:
                resp = await self._http.post(
                    f"/api/dostk/{category}", json=body, headers=headers)
            except httpx.HTTPError as exc:
                raise BrokerError(f"kiwoom http failure [{api_id}]: "
                                  f"{type(exc).__name__}") from exc

            if resp.status_code == 401:
                if reissued:
                    raise AuthError(f"token rejected after reissue [{api_id}]")
                logger.info("kiwoom 401 on %s — reissuing token", api_id)
                self._tokens.invalidate()
                reissued = True
                continue
            if resp.status_code == 429:
                await self._limiter.penalize(api_id)  # 서버 backpressure를 로컬 버킷에 반영
                if backoff_idx >= len(BACKOFF_SECONDS):
                    raise RateLimitError(f"rate limit exhausted [{api_id}]")
                wait = BACKOFF_SECONDS[backoff_idx]
                backoff_idx += 1
                logger.warning("kiwoom 429 on %s — backoff %.1fs", api_id, wait)
                await self._sleep(wait)
                continue

            try:
                data = resp.json()
            except ValueError as exc:
                raise BrokerError(f"kiwoom non-json response [{api_id}] "
                                  f"http={resp.status_code}") from exc
            if not isinstance(data, dict):
                raise BrokerError(f"kiwoom unexpected response shape [{api_id}]")
            code = data.get("return_code")
            if resp.status_code != 200 or (code is not None and code != 0):
                msg = str(data.get("return_msg"))
                if not reissued and "8005" in msg:
                    # 실측(2026-07-17): 키움은 무효 토큰을 HTTP 401이 아니라
                    # 200 + return_msg의 [8005]로 알린다 — 재발급 후 1회 재시도
                    logger.info("kiwoom token invalid (8005) on %s — reissuing", api_id)
                    self._tokens.invalidate()
                    reissued = True
                    continue
                raise ApiError(code if code is not None else resp.status_code,
                               msg, api_id)
            return (data,
                    resp.headers.get("cont-yn", "N"),
                    resp.headers.get("next-key", ""))

    async def call_paged(
        self, category: str, api_id: str, body: dict, max_pages: int = 50,
    ) -> AsyncIterator[dict]:
        cont_yn, next_key = "N", ""
        for _ in range(max_pages):
            data, cont_yn, next_key = await self.call(
                category, api_id, body, cont_yn=cont_yn, next_key=next_key)
            yield data
            if cont_yn != "Y":
                return
        logger.warning("kiwoom paging stopped at max_pages=%d [%s]", max_pages, api_id)

    async def aclose(self) -> None:
        try:
            if self._owns_tokens:
                await self._tokens.revoke()
        finally:
            if self._owns_http:
                await self._http.aclose()
