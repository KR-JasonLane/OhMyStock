"""/schedule API + lifespan 기동 게이트(P6 Task 6, 스펙 §5·§7).

핵심 회귀: ① 기존 lifespan 테스트가 스케줄러를 기동하지 않음(conftest
autouse — 보안 계획 리뷰), ② replay 게이트가 env enabled보다 우선,
③ pause/resume 401 계약(엔드포인트별 실제 조립 — test_api_security 전례),
④ status 응답의 reason 고정 리터럴 계약(실행 예외 경로 포함)."""

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.core.config import Settings
from app.domain.orchestration.config import ScheduleConfig
from app.domain.orchestration.service import SchedulerService
from app.domain.orchestration.timeline import Action, Job, Reason
from app.main import create_app
from app.store.analysis_store import AnalysisStore
from app.store.collection_store import CollectionStore
from app.store.models import Base
from app.store.scheduler_store import SchedulerStore
from app.store.scoring_store import ScoringStore
from app.store.trading_store import TradingStore

KST = timezone(timedelta(hours=9))
_ACTIONS = {a.value for a in Action}
_REASONS = {r.value for r in Reason}


def _settings(**overrides) -> Settings:
    base = dict(_env_file=None, kiwoom_app_key="AK", kiwoom_secret_key="SK",
                kiwoom_mock=True,
                database_url="sqlite+pysqlite:///:memory:")
    base.update(overrides)
    return Settings(**base)


# ── lifespan 기동 게이트 (스펙 §5) ─────────────────────────────────────

def test_lifespan_기본_테스트_부팅은_스케줄러_미기동():
    """conftest autouse(SCHEDULER_ENABLED=false)의 회귀 — 기존 lifespan
    테스트 전체가 실물 스케줄러 없이 돈다(보안 계획 리뷰)."""
    app = create_app(_settings())
    with TestClient(app):
        assert app.state.scheduler is None


def test_lifespan_enabled면_기동되고_셧다운에_정리된다(tmp_path):
    app = create_app(_settings(
        scheduler_enabled=True,
        database_url=f"sqlite+pysqlite:///{tmp_path / 's.db'}"))
    with TestClient(app):
        scheduler = app.state.scheduler
        assert scheduler is not None
        task = scheduler.current_task()
        assert task is not None and not task.done()
    assert task.done()               # 셧다운 — 스케줄러 태스크 정리 완료


def test_lifespan_replay_프로필은_enabled여도_미기동(tmp_path):
    """replay 게이트가 env보다 우선(스펙 §5 — 재생·실시계 혼합 방지)."""
    import respx
    settings = _settings(
        scheduler_enabled=True,
        kiwoom_base_url_override="http://127.0.0.1:9095",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'r.db'}")
    engine = create_engine(settings.database_url.get_secret_value())
    Base.metadata.create_all(engine)
    app = create_app(settings)
    with respx.mock:
        respx.get("http://127.0.0.1:9095/_replay/status").respond(
            json={"replay_now": "2026-07-10T09:00:00+09:00", "speed": 1.0})
        with TestClient(app):
            assert app.state.scheduler is None
            status = TestClient(app).get("/schedule/status").json()
            assert status == {"enabled": False, "reason": "replay_profile"}


# ── /schedule API 계약 (스펙 §7) ───────────────────────────────────────

def test_status_비활성이면_사유_리터럴():
    app = create_app(_settings())
    with TestClient(app) as client:
        body = client.get("/schedule/status").json()
        assert body == {"enabled": False, "reason": "disabled_by_env"}


def _mount_real_scheduler(app, tmp_path):
    """실제 SchedulerService+SchedulerStore(sqlite)를 앱에 장착하고 1틱 —
    reason 리터럴 계약을 fake가 아니라 실물 산출값으로 검증하기 위함."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'api.db'}")
    Base.metadata.create_all(engine)
    store = SchedulerStore(engine, CollectionStore(engine),
                           ScoringStore(engine), AnalysisStore(engine),
                           TradingStore(engine), run_environment="mock")

    class _Cal:
        KST = KST

        def is_trading_day(self, d: date) -> bool:
            return d.weekday() < 5

    scheduler = SchedulerService(
        {job: None for job in Job}, store, ScheduleConfig(), _Cal(),
        sleep=lambda _s: asyncio.sleep(0),
        now=lambda: datetime(2026, 7, 23, 9, 30, tzinfo=KST))
    app.state.scheduler = scheduler
    app.state.scheduler_store = store
    return scheduler


@pytest.mark.anyio
async def test_status_응답은_고정_리터럴만(tmp_path):
    """jobs.action/reason·recent_events.reason 전부 enum 값 집합 안 —
    자유 텍스트(예외 원문) 유입 차단 계약(보안 계획 리뷰)."""
    app = create_app(_settings())
    with TestClient(app) as client:
        scheduler = _mount_real_scheduler(app, tmp_path)
        await scheduler._tick()
        body = client.get("/schedule/status").json()
        assert body["enabled"] is True
        for job_state in body["jobs"].values():
            assert job_state["action"] in _ACTIONS
            assert job_state["reason"] in _REASONS
        for event in body["recent_events"]:
            assert event["action"] in _ACTIONS
            assert event["reason"] in _REASONS


# ── 인증 계약 (보안 계획 리뷰 — 엔드포인트별 실제 조립 401) ────────────

@pytest.mark.parametrize("path", ["/schedule/pause", "/schedule/resume"])
def test_pause_resume은_trade_토큰_없으면_401(path, tmp_path):
    app = create_app(_settings(api_trade_token="trade-secret"))
    with TestClient(app) as client:
        _mount_real_scheduler(app, tmp_path)
        assert client.post(path).status_code == 401
        assert client.post(path,
                           headers={"X-API-Key": "wrong"}).status_code == 401


def test_pause_resume_정상_토큰과_스케줄러_반영(tmp_path):
    app = create_app(_settings(api_trade_token="trade-secret"))
    with TestClient(app) as client:
        scheduler = _mount_real_scheduler(app, tmp_path)
        headers = {"X-API-Key": "trade-secret"}
        assert client.post("/schedule/pause",
                           headers=headers).json() == {"paused": True}
        assert scheduler.paused is True
        assert client.post("/schedule/resume",
                           headers=headers).json() == {"paused": False}
        assert scheduler.paused is False


def test_pause는_스케줄러_비활성이면_503():
    app = create_app(_settings(api_trade_token="trade-secret"))
    with TestClient(app) as client:
        response = client.post("/schedule/pause",
                               headers={"X-API-Key": "trade-secret"})
        assert response.status_code == 503
