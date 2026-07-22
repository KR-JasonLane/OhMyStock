"""BackgroundRunService 베이스 계약 검증 — 더미 서브클래스로 오케스트레이션
스캐폴딩(원자적 start, conflict_check, _running 수명주기, run() 예외)만
직접 확인한다. 파이프라인 본문(수집/스코어링)은 각 서비스 테스트가 맡는다."""

import asyncio
from datetime import datetime, timezone

import pytest

from app.core.background_service import BackgroundRunService, StopMode


class DummyService(BackgroundRunService):
    """_run()만 구현하는 최소 서브클래스 — boom=True면 예외를 던진다."""

    def __init__(self, conflict_check=None, boom: bool = False, now=None) -> None:
        super().__init__(task_label="dummy", conflict_check=conflict_check, now=now)
        self.ran = 0
        self._boom = boom
        self.accepted_calls = 0

    async def _run(self) -> None:
        self.ran += 1
        if self._boom:
            raise RuntimeError("boom")

    def _on_accepted(self) -> None:
        self.accepted_calls += 1


class StopAwareService(BackgroundRunService):
    """_run()이 사이클 경계에서 stop_requested()를 확인하는 상시 루프 모사."""

    def __init__(self, now=None) -> None:
        super().__init__(task_label="stopaware", now=now)
        self.cycles = 0

    async def _run(self) -> None:
        while self.stop_requested() is None and self.cycles < 100:
            self.cycles += 1
            await asyncio.sleep(0)  # 사이클 경계 — 이벤트 루프에 양보(정지 신호 반영 지점)


@pytest.mark.anyio
async def test_start는_연속_호출시_두번째를_거부한다():
    """check(_running)~set(_running=True) 사이에 await가 없어 원자적이다 —
    create_task는 스케줄만 할 뿐 즉시 실행하지 않으므로, 첫 start() 직후
    (이벤트 루프에 양보하기 전) 두번째 start()를 호출해도 안전하게 None이
    반환되어야 한다."""
    svc = DummyService()
    first = svc.start()
    second = svc.start()
    assert first is not None
    assert second is None
    assert svc.current_task() is first
    await first
    assert svc.is_running() is False
    assert svc.ran == 1


@pytest.mark.anyio
async def test_conflict_check가_참이면_start는_None을_반환한다():
    svc = DummyService(conflict_check=lambda: True)
    assert svc.start() is None
    assert svc.is_running() is False
    assert svc.ran == 0


@pytest.mark.anyio
async def test_run_내부_예외가_나도_is_running이_복원된다():
    """_execute()의 try/finally가 _run() 어디서 예외가 나든 구조적으로
    _running을 되돌린다 — create_run류 초기 실패도 별도 처리 없이 커버된다."""
    svc = DummyService(boom=True)
    with pytest.raises(RuntimeError, match="boom"):
        await svc.run()
    assert svc.is_running() is False


@pytest.mark.anyio
async def test_run은_중복_또는_충돌시_RuntimeError():
    svc = DummyService()
    task = svc.start()
    with pytest.raises(RuntimeError):
        await svc.run()
    await task

    conflicted = DummyService(conflict_check=lambda: True)
    with pytest.raises(RuntimeError, match="conflicting run in progress"):
        await conflicted.run()


@pytest.mark.anyio
async def test_on_accepted_훅은_가드_통과_직후에만_호출된다():
    """_on_accepted()는 start()/run()이 가드(중복/충돌 검사)를 통과했을 때만
    호출된다 — 거부된 호출(이미 실행 중, conflict_check=True)에서는 호출되지
    않아야 한다. 서브클래스가 실행별 상태를 세팅하는 유일한 안전 지점이라는
    계약을 검증한다."""
    svc = DummyService()
    assert svc.accepted_calls == 0

    task = svc.start()
    assert svc.accepted_calls == 1  # 가드 통과 → 호출됨

    second = svc.start()  # 이미 실행 중 — 거부
    assert second is None
    assert svc.accepted_calls == 1  # 거부된 호출은 훅을 호출하지 않음
    await task

    conflicted = DummyService(conflict_check=lambda: True)
    with pytest.raises(RuntimeError):
        await conflicted.run()
    assert conflicted.accepted_calls == 0  # conflict_check 거부도 호출 안 됨

    plain = DummyService()
    await plain.run()
    assert plain.accepted_calls == 1  # run()의 가드 통과 경로도 호출됨


# --- P5 Task 1: 타임스탬프 + 정지 계약 ---

@pytest.mark.anyio
async def test_started_finished_at은_now주입_시계로_고정된다():
    ticks = iter([datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc),
                  datetime(2026, 7, 22, 0, 5, tzinfo=timezone.utc)])
    svc = DummyService(now=lambda: next(ticks))
    assert svc.started_at() is None and svc.finished_at() is None
    await svc.run()
    assert svc.started_at() == datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)
    assert svc.finished_at() == datetime(2026, 7, 22, 0, 5, tzinfo=timezone.utc)


@pytest.mark.anyio
async def test_finished_at은_예외로_끝나도_고정된다():
    ticks = iter([datetime(2026, 7, 22, 1, 0, tzinfo=timezone.utc),
                  datetime(2026, 7, 22, 1, 1, tzinfo=timezone.utc)])
    svc = DummyService(boom=True, now=lambda: next(ticks))
    with pytest.raises(RuntimeError, match="boom"):
        await svc.run()
    assert svc.finished_at() == datetime(2026, 7, 22, 1, 1, tzinfo=timezone.utc)


@pytest.mark.anyio
async def test_request_stop이_상시루프를_협조적으로_종료시킨다():
    svc = StopAwareService()
    task = svc.start()
    # 루프가 몇 사이클 돌게 이벤트 루프에 양보
    await asyncio.sleep(0)
    svc.request_stop(StopMode.STOP_NEW_ENTRIES)
    await task
    assert svc.is_running() is False
    assert svc.stop_requested() is StopMode.STOP_NEW_ENTRIES
    assert svc.cycles < 100  # 100 상한 전에 정지 신호로 종료


@pytest.mark.anyio
async def test_start는_이전_정지신호를_클리어한다():
    svc = StopAwareService()
    svc.request_stop(StopMode.LIQUIDATE_ALL)
    assert svc.stop_requested() is StopMode.LIQUIDATE_ALL
    task = svc.start()  # 새 실행 — 정지 신호 리셋
    assert svc.stop_requested() is None
    await task
    assert svc.cycles == 100  # 정지 신호 없어 상한까지 돎


def test_기존_서브클래스는_정지신호를_확인안해도_무영향():
    # 정지 계약은 첨가형 — stop_requested를 안 보는 _run은 동작 불변.
    # DummyService(_run이 stop_requested 미확인)가 정상 동작함을 계약으로 명시.
    svc = DummyService()
    assert svc.stop_requested() is None  # 기본 None
