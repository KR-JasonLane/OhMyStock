"""실제 키움 모의서버 스모크. 실행: uv run pytest -m live -v
.env에 실제 발급 키 필요. KIWOOM_MOCK=true인 경우에만 실행된다."""

import httpx
import pytest

from app.adapters.kiwoom.auth import TokenManager
from app.core.config import Settings

pytestmark = pytest.mark.live

MOCK_BASE = "https://mockapi.kiwoom.com"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def settings() -> Settings:
    s = Settings()  # .env에서 로드
    if not s.kiwoom_mock:
        pytest.skip("라이브 스모크는 모의서버(KIWOOM_MOCK=true)에서만 실행한다")
    return s


@pytest.mark.anyio
async def test_live_토큰_발급과_폐기(settings):
    async with httpx.AsyncClient(base_url=MOCK_BASE, timeout=10) as http:
        tm = TokenManager(http, settings.kiwoom_app_key.get_secret_value(),
                           settings.kiwoom_secret_key.get_secret_value())
        token = await tm.get_token()
        assert token  # 값 자체는 출력하지 않는다
        await tm.revoke()
