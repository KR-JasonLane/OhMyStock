"""쓰기 엔드포인트(X-API-Key) 보호 계약 검증 — P3/P4 보안 패널 이월,
사용자 결정 2026-07-18(#24): API 키 + CORS 오리진 제한.

create_app()으로 실제 조립(라우터 + lifespan)을 그대로 사용해 /collect의
require_write_token 의존성 배선을 검증한다 (test_api_collect.py와 동일
패턴 — StubService로 하위 서비스만 교체).
"""

import logging

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture(autouse=True)
def _main_logger_enabled():
    """alembic 마이그레이션 테스트(tests/store/test_models_migration.py)가
    fileConfig(disable_existing_loggers=True 기본값)로 alembic.ini를 로드하면,
    ini에 명시되지 않은 기존 로거(app.main 포함)가 세션 내내 비활성화된다 —
    테스트 실행 순서에 따라 caplog가 기동 경고를 못 잡는 현상으로 나타남
    (test_collection_service.py의 동일 패턴 참고). 이 모듈의 로거만 명시적으로
    재활성화해 순서 무관하게 만든다."""
    logging.getLogger("app.main").disabled = False


def _settings(api_write_token: str | None = None) -> Settings:
    return Settings(_env_file=None, kiwoom_app_key="AK", kiwoom_secret_key="SK",
                    kiwoom_mock=True, database_url="sqlite+pysqlite:///:memory:",
                    api_write_token=api_write_token)


class StubService:
    """CollectionService의 start()/current_task()/progress() 계약을 흉내낸다
    (test_api_collect.py의 StubService와 동일 — lifespan 종료 시
    current_task()가 호출되므로 구현 필요)."""

    def __init__(self, running=False, progress=None):
        self._running = running
        self._progress = progress

    def start(self, warning=None):
        if self._running:
            return None
        self._running = True
        return object()

    def current_task(self):
        return None

    def progress(self):
        return self._progress

    def is_running(self):
        return self._running


def test_토큰_설정_헤더_없으면_401():
    app = create_app(_settings(api_write_token="test-token"))
    with TestClient(app) as client:
        app.state.collection = StubService()
        resp = client.post("/collect")
    assert resp.status_code == 401


def test_토큰_설정_잘못된_헤더면_401():
    app = create_app(_settings(api_write_token="test-token"))
    with TestClient(app) as client:
        app.state.collection = StubService()
        resp = client.post("/collect", headers={"X-API-Key": "wrong-token"})
    assert resp.status_code == 401


def test_토큰_설정_올바른_헤더면_통과():
    app = create_app(_settings(api_write_token="test-token"))
    with TestClient(app) as client:
        app.state.collection = StubService()
        resp = client.post("/collect", headers={"X-API-Key": "test-token"})
    assert resp.status_code == 202


def test_토큰_미설정이면_통과하고_기동시_경고를_남긴다(caplog):
    with caplog.at_level(logging.WARNING):
        app = create_app(_settings(api_write_token=None))
        with TestClient(app) as client:
            app.state.collection = StubService()
            resp = client.post("/collect")
    assert resp.status_code == 202
    assert any("api_write_token" in record.message for record in caplog.records)


def test_GET_status는_토큰_설정과_무관하게_열려있다():
    app = create_app(_settings(api_write_token="test-token"))
    with TestClient(app) as client:
        app.state.collection = StubService()
        resp = client.get("/collect/status")
    assert resp.status_code == 200
