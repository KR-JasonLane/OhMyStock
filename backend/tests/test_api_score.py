"""/score API 계약 검증 — 가짜 서비스/스토어로 상태별 응답 코드 확인."""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.score import router
from app.core.config import Settings
from app.domain.scoring.service import ScoringProgress


class FakeScoring:
    def __init__(self, running=False, progress=None, latest=None):
        self._running = running
        self._progress = progress
        self._latest = latest

    def is_running(self):
        return self._running

    def start(self):
        return None if self._running else object()

    def progress(self):
        return self._progress

    def latest_results(self):
        return self._latest


class FakeCollection:
    def __init__(self, running=False):
        self._running = running

    def is_running(self):
        return self._running


class BrokenScoring(FakeScoring):
    """score_runs.config 손상(DB 변조 등) 시나리오 재현용 — latest_results가
    json.JSONDecodeError를 던진다 (T6 보안 캐리오버, T7에서 delegate 경유로 갱신)."""

    def latest_results(self):
        raise json.JSONDecodeError("Expecting value", "", 0)


def make_client(scoring=None, collection=None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.scoring = scoring or FakeScoring()
    app.state.collection = collection or FakeCollection()
    # require_write_token 의존성이 app.state.settings를 읽는다 — 토큰
    # 미설정이면 차단하지 않으므로 이 미니 앱의 기존 기대 동작은 그대로다
    # (보호 자체의 동작 검증은 test_api_security.py).
    app.state.settings = Settings(_env_file=None, kiwoom_app_key="AK",
                                  kiwoom_secret_key="SK",
                                  database_url="sqlite+pysqlite:///:memory:")
    return TestClient(app)


def test_score_시작():
    resp = make_client().post("/score")
    assert resp.status_code == 202
    assert resp.json() == {"started": True}


def test_score_중복이면_409():
    resp = make_client(scoring=FakeScoring(running=True)).post("/score")
    assert resp.status_code == 409


def test_수집중이면_409():
    resp = make_client(collection=FakeCollection(running=True)).post("/score")
    assert resp.status_code == 409
    assert "collection" in resp.json()["detail"]


def test_status_idle():
    resp = make_client().get("/score/status")
    assert resp.json() == {"status": "idle"}


def test_status_실패사유_노출():
    progress = ScoringProgress(run_id=3, status="failed", stage="finished",
                               done=0, total=100, failure_reason="stale data")
    resp = make_client(scoring=FakeScoring(progress=progress)).get("/score/status")
    body = resp.json()
    assert body["status"] == "failed" and body["failure_reason"] == "stale data"


def test_latest_없으면_404():
    assert make_client().get("/score/latest").status_code == 404


def test_latest_반환():
    latest = {"run_id": 7, "candidates": []}
    resp = make_client(scoring=FakeScoring(latest=latest)).get("/score/latest")
    assert resp.status_code == 200 and resp.json() == latest


def test_latest_손상된_config_JSON은_스택트레이스_없는_500():
    resp = make_client(scoring=BrokenScoring()).get("/score/latest")
    assert resp.status_code == 500
    body = resp.json()
    assert body == {"detail": "stored scoring config is corrupted"}
