import pytest

from app.adapters.kiwoom.rate_limiter import RateLimiter


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


@pytest.mark.anyio
async def test_버스트_한도까지는_대기없이_통과한다():
    c = FakeClock()
    rl = RateLimiter(rate=1.0, burst=2, clock=c.now, sleep=c.sleep)
    await rl.acquire("ka10081")
    await rl.acquire("ka10081")
    assert c.sleeps == []


@pytest.mark.anyio
async def test_버스트_초과시_보충될_때까지_대기한다():
    c = FakeClock()
    rl = RateLimiter(rate=1.0, burst=2, clock=c.now, sleep=c.sleep)
    for _ in range(3):
        await rl.acquire("ka10081")
    assert c.sleeps == [pytest.approx(1.0)]  # 3번째는 1토큰 보충(1초) 대기


@pytest.mark.anyio
async def test_TR별로_버킷이_독립이다():
    c = FakeClock()
    rl = RateLimiter(rate=1.0, burst=1, clock=c.now, sleep=c.sleep)
    await rl.acquire("ka10081")
    await rl.acquire("ka10001")  # 다른 TR — 대기 없음
    assert c.sleeps == []


@pytest.mark.anyio
async def test_시간이_지나면_토큰이_보충된다():
    c = FakeClock()
    rl = RateLimiter(rate=1.0, burst=1, clock=c.now, sleep=c.sleep)
    await rl.acquire("ka10081")
    c.t += 1.0
    await rl.acquire("ka10081")
    assert c.sleeps == []


@pytest.mark.anyio
async def test_동시성_한_TR의_대기가_다른_TR을_막지_않는다():
    import asyncio
    import time

    rl = RateLimiter(rate=1.0, burst=1)  # 실제 clock/sleep

    async def slow():
        await rl.acquire("ka10081")
        await rl.acquire("ka10081")  # 버킷 고갈 → ~1초 대기

    async def fast():
        await asyncio.sleep(0.05)    # slow가 대기에 들어간 뒤 실행되도록
        t0 = time.monotonic()
        await rl.acquire("kt10000")  # 독립 버킷 — 즉시 통과해야 함
        return time.monotonic() - t0

    _, fast_elapsed = await asyncio.gather(slow(), fast())
    assert fast_elapsed < 0.5  # 락에 갇히면 ~0.95초가 걸린다


def test_생성자_rate_검증():
    with pytest.raises(ValueError):
        RateLimiter(rate=0)


def test_생성자_burst_검증():
    with pytest.raises(ValueError):
        RateLimiter(burst=0)


@pytest.mark.anyio
async def test_penalize_후_acquire가_대기한다():
    c = FakeClock()
    rl = RateLimiter(rate=1.0, burst=2, clock=c.now, sleep=c.sleep)
    await rl.penalize("ka10081")  # 버킷을 0으로 비운다
    await rl.acquire("ka10081")
    assert c.sleeps == [pytest.approx(1.0)]
