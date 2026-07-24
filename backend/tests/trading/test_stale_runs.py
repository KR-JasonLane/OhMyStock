"""좀비 run 정정(P6 Task 7d) — 기동 시 잔존 'running' 행을 stopped로.

배경(2026-07-24 실측): 컨테이너 교체 중 graceful 타임아웃 초과로
lifespan finally가 안 돌아 trade_runs에 status='running' 고아 행이 남고,
그 run의 warnings(0012)가 통째로 소실됐다 — Task 7c의 목적이 크래시
경로에서 무력화되던 지점. 스케줄러 판정은 좀비를 무시해 데드락은 없었다
(설계대로)."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.core.config import Settings
from app.main import create_app
from app.store.models import Base
from app.store.trading_store import TradingStore

KST = timezone(timedelta(hours=9))
T0 = datetime(2026, 7, 24, 2, 0, tzinfo=timezone.utc)


def _store(tmp_path, name="stale.db") -> TradingStore:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    return TradingStore(engine, now=lambda: T0)


def test_running_run을_stopped로_정정한다(tmp_path):
    store = _store(tmp_path)
    zombie = store.create_run("{}", "mock")
    assert store.close_stale_runs("mock") == 1
    latest = store.latest_run()
    assert latest["run_id"] == zombie
    assert latest["status"] == "stopped"
    assert latest["stopped_by_kill_switch"] is False   # 킬스위치 아님
    assert latest["failure_reason"] == "process_restart"
    assert latest["finished_at"] is not None


def test_정정된_run은_재기동_대상으로_남는다(tmp_path):
    """§4-d: stopped AND NOT kill_switch = 미완료 — 스케줄러가 캐치업으로
    재기동한다(좀비를 '완료'로 만들면 그날 트레이딩이 영구 스킵된다)."""
    store = _store(tmp_path, "stale2.db")
    store.create_run("{}", "mock")
    store.close_stale_runs("mock")
    day = T0.astimezone(KST).date()
    assert store.has_completed_run(day, "mock") is False
    assert store.last_failed_finished_at(day, "mock") is not None


def test_종료된_run과_타_환경은_건드리지_않는다(tmp_path):
    store = _store(tmp_path, "stale3.db")
    done = store.create_run("{}", "mock")
    store.finish_run(done, "succeeded")
    killed = store.create_run("{}", "mock")
    store.finish_run(killed, "stopped", stopped_by_kill_switch=True,
                     kill_switch_mode="liquidate_all")
    store.create_run("{}", "replay")          # 타 환경 좀비 — 보존
    assert store.close_stale_runs("mock") == 0
    assert store.close_stale_runs("replay") == 1


def test_킬스위치_요청_직후_크래시는_킬스위치로_정정된다(tmp_path):
    """보안 T7d Important — 정지는 협조적이라 finish_run 전에 죽을 수 있다.
    요청이 DB에 먼저 남아 있으면(record_stop_request) 좀비 정정이 이를
    운영자 의사로 인정해 **그날 자동 재기동을 막는다**(Task 7a 보장)."""
    store = _store(tmp_path, "stale5.db")
    run_id = store.create_run("{}", "mock")
    store.record_stop_request(run_id, "liquidate_all")   # 킬스위치 요청
    # ... finish_run 전에 프로세스 크래시 ...
    assert store.close_stale_runs("mock") == 1
    latest = store.latest_run()
    assert latest["status"] == "stopped"
    assert latest["stopped_by_kill_switch"] is True      # 운영자 의사 보존
    assert latest["failure_reason"] == "kill_switch_before_crash"
    day = T0.astimezone(KST).date()
    assert store.has_completed_run(day, "mock") is True  # 재기동 대상 아님


@pytest.mark.anyio
async def test_service_request_stop_durable이_DB에_남긴다(tmp_path):
    """실배선 회귀 — API 진입점(`request_stop_durable`)이 인메모리 정지와
    DB 영속을 함께 수행하는지. 동기 `request_stop`(베이스 계약)은 인메모리
    만 — store 호출을 그 안에 두면 이벤트 루프를 블로킹한다(아키텍트 T7d)."""
    from app.core.background_service import BackgroundRunService, StopMode
    from app.domain.trading.service import TradingService

    store = _store(tmp_path, "stale6.db")
    run_id = store.create_run("{}", "mock")
    svc = TradingService.__new__(TradingService)   # 순수 배선 검증
    BackgroundRunService.__init__(svc, "trading")
    svc._store = store
    svc._run_id = run_id

    svc.request_stop(StopMode.STOP_NEW_ENTRIES)    # 동기 경로: 인메모리만
    assert svc.stop_requested() is StopMode.STOP_NEW_ENTRIES
    assert store.latest_run()["kill_switch_mode"] is None

    await svc.request_stop_durable(StopMode.LIQUIDATE_ALL)
    assert svc.stop_requested() is StopMode.LIQUIDATE_ALL
    assert store.latest_run()["kill_switch_mode"] == "liquidate_all"


def test_좀비_없으면_0(tmp_path):
    store = _store(tmp_path, "stale4.db")
    assert store.close_stale_runs("mock") == 0


def test_lifespan_기동이_좀비를_정정한다(tmp_path):
    """실배선 회귀 — create_app lifespan 경로에서 실제로 호출되는지."""
    db = tmp_path / "lifespan.db"
    engine = create_engine(f"sqlite+pysqlite:///{db}")
    Base.metadata.create_all(engine)
    zombie = TradingStore(engine).create_run("{}", "mock")
    settings = Settings(_env_file=None, kiwoom_app_key="AK",
                        kiwoom_secret_key="SK", kiwoom_mock=True,
                        database_url=f"sqlite+pysqlite:///{db}")
    with TestClient(create_app(settings)):
        pass
    latest = TradingStore(engine).latest_run()
    assert latest["run_id"] == zombie
    assert latest["status"] == "stopped"
    assert latest["failure_reason"] == "process_restart"
