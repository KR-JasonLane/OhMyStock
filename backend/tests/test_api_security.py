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
    재활성화해 순서 무관하게 만든다. app.api.security도 모듈 임포트 시점에
    생성되는 로거라 동일한 함정에 걸릴 수 있어 함께 재활성화한다(auth 거부
    로그 caplog 검증에 필요)."""
    logging.getLogger("app.main").disabled = False
    logging.getLogger("app.api.security").disabled = False


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


@pytest.mark.parametrize("path", ["/collect", "/score", "/analyze"])
def test_토큰_설정_헤더_없으면_401(path):
    # 세 쓰기 엔드포인트 모두 동일한 require_write_token 의존성을 쓰지만,
    # 라우터가 실제로 배선을 빠뜨리지 않았는지는 각각 실제 조립으로
    # 확인해야 한다(dev 패널 지적 — /collect만 검증하고 나머지를
    # 누락하면 회귀를 못 잡는다).
    app = create_app(_settings(api_write_token="test-token"))
    with TestClient(app) as client:
        resp = client.post(path)
    assert resp.status_code == 401


def test_토큰_설정_잘못된_헤더면_401(caplog):
    app = create_app(_settings(api_write_token="test-token"))
    with caplog.at_level(logging.WARNING):
        with TestClient(app) as client:
            app.state.collection = StubService()
            resp = client.post("/collect", headers={"X-API-Key": "wrong-token"})
    assert resp.status_code == 401
    # 서버 로그에 거부 사유(path/reason)는 남기되 토큰 값은 절대 남기지
    # 않는다 (trader 패널 Important #2).
    warnings = [r for r in caplog.records if r.name == "app.api.security"]
    assert len(warnings) == 1
    assert warnings[0].getMessage() == (
        "write endpoint auth rejected: path=/collect reason=mismatch")
    assert "test-token" not in warnings[0].getMessage()
    assert "wrong-token" not in warnings[0].getMessage()


def test_토큰_설정_비ASCII_헤더면_401_이지_500이_아니다():
    # secrets.compare_digest(str, str)는 비-ASCII가 섞이면 TypeError를
    # 던져 500으로 샐 수 있다(보안 패널 Minor) — 바이트 비교로 우회했는지
    # 회귀 검증. httpx 클라이언트 자체가 str 헤더값을 ascii로만 인코딩하므로
    # (일반 문자열로는 이 결함을 재현할 수 없음), Starlette가 요청 헤더를
    # latin-1로 디코딩하는 경로(ASGI 스펙)를 그대로 이용해 raw 바이트를
    # 헤더 값으로 보낸다 — 이러면 서버 쪽에서 비-ASCII str이 실제로
    # 만들어진다.
    app = create_app(_settings(api_write_token="test-token"))
    with TestClient(app) as client:
        app.state.collection = StubService()
        resp = client.post("/collect", headers={"X-API-Key": b"\xff\xfe\xfd"})
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


@pytest.mark.parametrize("path", ["/collect/status", "/score/status", "/analyze/status"])
def test_GET_status는_토큰_설정과_무관하게_열려있다(path):
    # /score, /analyze의 status GET은 lifespan이 만든 실제 ScoringService/
    # AnalysisService를 그대로 쓴다(app.state 오버라이드 불필요) — 두
    # 서비스 모두 progress()가 미실행 상태에서 None을 반환하는 계약
    # (domain/scoring/service.py, domain/analysis/service.py)이라 idle
    # 응답이 정상적으로 나온다. 여기서 검증할 계약은 "인증 없이 200대
    # 응답이 온다"는 것뿐이므로, 세부 응답 바디는 각 라우터의 전용
    # 테스트(test_api_score.py 등)에 맡기고 401이 아님만 확인한다.
    app = create_app(_settings(api_write_token="test-token"))
    with TestClient(app) as client:
        app.state.collection = StubService()
        resp = client.get(path)
    assert resp.status_code != 401
