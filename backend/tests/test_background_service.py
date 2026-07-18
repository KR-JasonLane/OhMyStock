"""BackgroundRunService 베이스 계약 검증 — 더미 서브클래스로 오케스트레이션
스캐폴딩(원자적 start, conflict_check, _running 수명주기, run() 예외)만
직접 확인한다. 파이프라인 본문(수집/스코어링)은 각 서비스 테스트가 맡는다."""

import pytest

from app.core.background_service import BackgroundRunService


class DummyService(BackgroundRunService):
    """_run()만 구현하는 최소 서브클래스 — boom=True면 예외를 던진다."""

    def __init__(self, conflict_check=None, boom: bool = False) -> None:
        super().__init__(task_label="dummy", conflict_check=conflict_check)
        self.ran = 0
        self._boom = boom

    async def _run(self) -> None:
        self.ran += 1
        if self._boom:
            raise RuntimeError("boom")


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
