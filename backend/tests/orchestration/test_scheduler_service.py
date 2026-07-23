"""SchedulerService 틱 루프(P6 Task 5, 스펙 §5·§8).

가짜 시계·가짜 서비스(Protocol)·가짜 store로 실행 계층만 검증 — 판정은
timeline 테스트가 소유. 핵심: TRIGGER 실행/START_REJECTED, SKIP·GAVE_UP의
하루 1회 dedup, pause, 틱 예외 생존, 재기동 예산 1회 소진 후 dead."""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import pytest

from app.domain.orchestration.config import ScheduleConfig
from app.domain.orchestration.service import SchedulerService
from app.domain.orchestration.timeline import Action, Job, JobFacts, Reason

KST = timezone(timedelta(hours=9))
THU = date(2026, 7, 23)


class Cal:
    KST = KST

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5


def _dt(h: int, m: int) -> datetime:
    return datetime(2026, 7, 23, h, m, tzinfo=KST)


class FakeService:
    def __init__(self, accept=True, raise_on_start=False):
        self.starts = 0
        self._accept = accept
        self._raise = raise_on_start
        self.running = False

    def start(self):
        self.starts += 1
        if self._raise:
            raise RuntimeError("db down: dsn=postgres://user:pw@host")
        if not self._accept:
            return None
        return asyncio.create_task(asyncio.sleep(0))

    def is_running(self) -> bool:
        return self.running


class FakeStore:
    def __init__(self, facts=None):
        self.facts = facts or {job: JobFacts() for job in Job}
        self.events: list[tuple] = []
        self.build_calls = 0
        self.fail_build = False

    def build_job_facts(self, reference, today):
        self.build_calls += 1
        if self.fail_build:
            raise ConnectionError("db unreachable")
        return dict(self.facts)

    def record_event(self, job, action, reason, run_id=None):
        self.events.append((job, action, reason))


def _svc(now, *, facts=None, trade=None, others_done=True):
    """기본: collect/score/analyze 완료(수요일 몫), trade만 시나리오 대상."""
    store = FakeStore(facts)
    if facts is None and others_done:
        for job in (Job.COLLECT, Job.SCORE, Job.ANALYZE):
            store.facts[job] = JobFacts(completed=True)
    services = {Job.COLLECT: FakeService(), Job.SCORE: FakeService(),
                Job.ANALYZE: FakeService(), Job.TRADE: trade}
    scheduler = SchedulerService(services, store, ScheduleConfig(), Cal(),
                                 sleep=lambda _s: asyncio.sleep(0),
                                 now=lambda: now)
    return scheduler, store, services


@pytest.mark.anyio
async def test_트리거_실행과_이벤트_기록():
    trade = FakeService()
    scheduler, store, _ = _svc(_dt(9, 30), trade=trade)
    await scheduler._tick()
    assert trade.starts == 1
    assert (Job.TRADE, Action.TRIGGER, Reason.FIRST_ATTEMPT) in store.events


@pytest.mark.anyio
async def test_start_거부는_START_REJECTED_CONFLICT_1회_dedup():
    trade = FakeService(accept=False)
    scheduler, store, _ = _svc(_dt(9, 30), trade=trade)
    await scheduler._tick()
    await scheduler._tick()          # 두 틱 — 이벤트는 1건(dedup)
    assert trade.starts == 2         # 재시도 자체는 매 틱(백오프 없는 일시 상태)
    rejected = [e for e in store.events
                if e[1] is Action.START_REJECTED]
    assert rejected == [(Job.TRADE, Action.START_REJECTED, Reason.CONFLICT)]


@pytest.mark.anyio
async def test_엔진_미조립_SKIP은_하루_1회_기록():
    scheduler, store, _ = _svc(_dt(9, 30), trade=None)
    await scheduler._tick()
    await scheduler._tick()
    skips = [e for e in store.events if e[1] is Action.SKIP]
    assert skips == [(Job.TRADE, Action.SKIP, Reason.ENGINE_NOT_ASSEMBLED)]


@pytest.mark.anyio
async def test_실행중_서비스는_facts에_덧입혀져_재트리거_안_함():
    trade = FakeService()
    trade.running = True
    scheduler, store, _ = _svc(_dt(9, 30), trade=trade)
    await scheduler._tick()
    assert trade.starts == 0
    assert store.events == []        # ALREADY_RUNNING은 WAIT — 이벤트 비대상


@pytest.mark.anyio
async def test_pause는_전_잡_무동작_resume으로_재개():
    trade = FakeService()
    scheduler, store, _ = _svc(_dt(9, 30), trade=trade)
    scheduler.pause()
    await scheduler._tick()
    assert trade.starts == 0 and store.events == []
    scheduler.resume()
    await scheduler._tick()
    assert trade.starts == 1


@pytest.mark.anyio
async def test_start_예외는_EXECUTION_ERROR_이벤트_나머지_잡_계속(caplog):
    """예외 원문(dsn 포함)은 로그 전용 — 이벤트에는 고정 리터럴만."""
    trade = FakeService(raise_on_start=True)
    scheduler, store, _ = _svc(_dt(19, 30), trade=trade)
    # 저녁: collect 미완(트리거 대상), trade는 창 밖이지만 시나리오 단순화를
    # 위해 낮 시각으로 별도 틱
    scheduler2, store2, _ = _svc(_dt(9, 30), trade=trade)
    with caplog.at_level(logging.ERROR):
        await scheduler2._tick()
    assert (Job.TRADE, Action.START_REJECTED,
            Reason.EXECUTION_ERROR) in store2.events
    assert all("dsn" not in str(e) for e in store2.events)


async def _until(cond, timeout_s: float = 2.0) -> bool:
    """조건 충족까지 실시간 폴링 — to_thread 완료는 이벤트루프 yield 횟수로
    결정론이 보장되지 않는다(보안 T5 관측: sleep(0) 고정 횟수 대기는 8회 중
    1회꼴 플래키)."""
    for _ in range(int(timeout_s / 0.005)):
        if cond():
            return True
        await asyncio.sleep(0.005)
    return False


@pytest.mark.anyio
async def test_틱_실패는_루프를_죽이지_않는다():
    trade = FakeService()
    scheduler, store, _ = _svc(_dt(9, 30), trade=trade)
    store.fail_build = True
    scheduler.start()
    assert await _until(lambda: store.build_calls >= 1)   # 첫 틱 실패 소화
    store.fail_build = False
    assert await _until(lambda: trade.starts >= 1)        # 다음 틱 정상 트리거
    task = scheduler.current_task()
    assert not task.done()           # 루프 생존
    assert scheduler.dead is False
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.anyio
async def test_재기동_예산은_프로세스당_1회_소진_후_dead():
    class Crashing(SchedulerService):
        async def _loop(self):
            raise RuntimeError("boom")

    scheduler, _store, _ = _svc(_dt(9, 30), trade=FakeService())
    crashing = Crashing({job: FakeService() for job in Job}, FakeStore(),
                        ScheduleConfig(), Cal(), sleep=lambda _s:
                        asyncio.sleep(0), now=lambda: _dt(9, 30))
    crashing.start()
    for _ in range(10):
        await asyncio.sleep(0)       # 1차 사망 → 재기동 → 2차 사망
    assert crashing.dead is True     # 예산(1회) 소진 — 영속 dead


@pytest.mark.anyio
async def test_snapshot은_고정_리터럴만_노출():
    trade = FakeService()
    scheduler, _store, _ = _svc(_dt(9, 30), trade=trade)
    await scheduler._tick()
    snap = scheduler.snapshot()
    assert snap["paused"] is False and snap["dead"] is False
    assert snap["jobs"]["trade"] == {"action": "triggered",
                                     "reason": "first_attempt",
                                     "next_attempt_at": None}
    assert snap["jobs"]["collect"]["reason"] == "completed"


@pytest.mark.anyio
async def test_snapshot_예정_시각_힌트(caplog):
    """스펙 §7 "예정 시각"(아키텍트 T5 Important) — 창 미개장은 창 열림
    시각, 백오프 대기는 실패+백오프 시각(KST ISO)."""
    trade = FakeService()
    scheduler, store, _ = _svc(_dt(8, 30), trade=trade)   # 09:00 전
    await scheduler._tick()
    snap = scheduler.snapshot()
    assert snap["jobs"]["trade"]["reason"] == "window_not_open"
    assert snap["jobs"]["trade"]["next_attempt_at"].startswith(
        "2026-07-23T09:00:00")
    store.facts[Job.TRADE] = JobFacts(
        last_failure_at=datetime(2026, 7, 23, 9, 30, tzinfo=KST))
    scheduler2, store2, _ = _svc(_dt(9, 30), trade=trade)
    store2.facts[Job.TRADE] = JobFacts(
        last_failure_at=datetime(2026, 7, 23, 9, 30, tzinfo=KST))
    await scheduler2._tick()
    hint = scheduler2.snapshot()["jobs"]["trade"]["next_attempt_at"]
    assert hint.startswith("2026-07-23T09:31:00")         # +60초 백오프


@pytest.mark.anyio
async def test_이중_start는_거부():
    scheduler, *_ = _svc(_dt(9, 30), trade=FakeService())
    scheduler.start()
    with pytest.raises(RuntimeError, match="already started"):
        scheduler.start()
    task = scheduler.current_task()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.anyio
async def test_dedup은_날짜_롤오버에_리셋(caplog):
    """(job, action, reason, 날짜) dedup 키의 자정 리셋 회귀(개발자 T5) —
    어제 기록된 SKIP이 오늘 몫에서는 새로 기록(이벤트+로그)돼야 한다."""
    clock = {"now": _dt(9, 30)}
    store = FakeStore()
    for job in (Job.COLLECT, Job.SCORE, Job.ANALYZE):
        store.facts[job] = JobFacts(completed=True)
    services = {Job.COLLECT: FakeService(), Job.SCORE: FakeService(),
                Job.ANALYZE: FakeService(), Job.TRADE: None}   # SKIP 대상
    scheduler = SchedulerService(services, store, ScheduleConfig(), Cal(),
                                 sleep=lambda _s: asyncio.sleep(0),
                                 now=lambda: clock["now"])
    await scheduler._tick()
    await scheduler._tick()
    skips = [e for e in store.events if e[1] is Action.SKIP]
    assert len(skips) == 1                     # 당일 dedup
    clock["now"] = datetime(2026, 7, 24, 9, 30, tzinfo=KST)  # 금요일
    await scheduler._tick()
    skips = [e for e in store.events if e[1] is Action.SKIP]
    assert len(skips) == 2                     # 몫 전환 — 새로 1회 기록
