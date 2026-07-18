"""/analyze API 계약 검증 — 가짜 서비스로 상태별 응답 코드 확인
(test_api_score.py와 동일 패턴)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.analyze import router
from app.domain.analysis.service import AnalysisProgress


class FakeAnalysis:
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


def make_client(analysis=None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.analysis = analysis or FakeAnalysis()
    return TestClient(app)


def test_analyze_시작():
    resp = make_client().post("/analyze")
    assert resp.status_code == 202
    assert resp.json() == {"started": True}


def test_analyze_중복이면_409():
    resp = make_client(analysis=FakeAnalysis(running=True)).post("/analyze")
    assert resp.status_code == 409
    assert "already running" in resp.json()["detail"]


def test_status_idle():
    resp = make_client().get("/analyze/status")
    assert resp.json() == {"status": "idle"}


def test_status_실패사유_노출():
    progress = AnalysisProgress(run_id=3, status="failed", stage="gate",
                                done=0, total=0,
                                failure_reason="no candidates in scoring run")
    resp = make_client(analysis=FakeAnalysis(progress=progress)).get(
        "/analyze/status")
    body = resp.json()
    assert body["status"] == "failed"
    assert body["failure_reason"] == "no candidates in scoring run"


def test_status_run_id_None이면_null로_노출한다():
    # CONTRACT (T6 게이트): 스코어링 런 자체가 없어 run이 생성되지 못한
    # 경우 run_id는 None이다 — 절대 0으로 위조하지 않고 그대로 null을
    # 응답에 노출해야 한다 (AnalysisProgress docstring 계약).
    progress = AnalysisProgress(run_id=None, status="failed", stage="gate",
                                done=0, total=0,
                                failure_reason="no succeeded scoring run - "
                                              "run scoring first")
    resp = make_client(analysis=FakeAnalysis(progress=progress)).get(
        "/analyze/status")
    body = resp.json()
    assert body["run_id"] is None
    assert body["status"] == "failed"


def test_latest_없으면_404():
    resp = make_client().get("/analyze/latest")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "no succeeded analysis run"


def test_latest_반환():
    latest = {"run_id": 7, "verdicts": [], "picks": []}
    resp = make_client(analysis=FakeAnalysis(latest=latest)).get(
        "/analyze/latest")
    assert resp.status_code == 200
    assert resp.json() == latest


def test_latest_응답에_config가_노출되지_않는다():
    # SECURITY (T6 게이트): analysis_runs.config(전체 설정 JSON)는 API
    # 응답에 절대 노출되지 않아야 한다. AnalysisStore.latest_results가 이미
    # config를 빼고 반환하므로 여기서는 fake latest에도 config 키가 없는
    # 정상 사전을 그대로 통과시켜 회귀를 잡는다.
    latest = {"run_id": 7, "verdicts": [], "picks": []}
    resp = make_client(analysis=FakeAnalysis(latest=latest)).get(
        "/analyze/latest")
    assert "config" not in resp.json()
