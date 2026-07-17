"""TR(api-id)별 토큰버킷. 키움 레이트리밋(비공식: TR당 ~1 req/s, burst ~2) 준수용.
수치는 실측 후 조정할 수 있도록 생성자 인자로 열어둔다."""

import asyncio
import time
from collections.abc import Awaitable, Callable


class _Bucket:
    __slots__ = ("tokens", "last")

    def __init__(self, tokens: float, last: float) -> None:
        self.tokens = tokens
        self.last = last


class RateLimiter:
    def __init__(
        self,
        rate: float = 1.0,
        burst: int = 2,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if rate <= 0 or burst < 1:
            raise ValueError("rate must be > 0 and burst >= 1")
        self._rate = rate
        self._burst = float(burst)
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, tr_id: str) -> None:
        while True:
            async with self._lock:
                now = self._clock()
                bucket = self._buckets.setdefault(tr_id, _Bucket(self._burst, now))
                bucket.tokens = min(self._burst,
                                    bucket.tokens + (now - bucket.last) * self._rate)
                bucket.last = now
                if bucket.tokens >= 1.0:
                    bucket.tokens -= 1.0
                    return
                wait = (1.0 - bucket.tokens) / self._rate
            await self._sleep(wait)  # 락 밖 대기 — 다른 TR을 막지 않는다

    async def penalize(self, tr_id: str) -> None:
        """서버 429 수신 시 로컬 버킷을 비워 즉시 재돌진을 막는다."""
        async with self._lock:
            self._buckets[tr_id] = _Bucket(0.0, self._clock())
