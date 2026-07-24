import pytest

from app.core.config import Settings


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
    # 실제 backend/.env의 값이 환경변수 삭제 뒤 다시 로드되지 않게 dotenv
    # source 자체를 테스트 동안 차단한다.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    # 일반 테스트와 lifespan 테스트는 Telegram 자격 증명을 상속하지 않는다.
    # Telegram client 단위 테스트만 명시적 가짜 transport로 생성한다.
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_ID", raising=False)
