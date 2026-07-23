import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _scheduler_disabled_by_default(monkeypatch):
    """테스트 부팅 기본 차단(P6 보안 계획 리뷰 — 스펙 §5): lifespan을
    실제로 도는 테스트가 실물 스케줄러(→ 수집 잡이 실 네트워크 호출 유발
    가능)를 띄우지 않게 한다. 기동 경로 테스트는
    Settings(scheduler_enabled=True) 명시 주입으로 우회한다(explicit
    kwarg가 env보다 우선 — pydantic-settings)."""
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
