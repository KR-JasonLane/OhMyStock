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
    # 전송 경로만 검증한다: /analyze/latest가 service.latest_results()의
    # 반환값을 그대로 통과시키는지 (config 키가 없는 정상 사전을 fake로
    # 넘겨 응답에도 없는지 확인). 실제 config 비노출 보장의 게이트는
    # AnalysisStore 레벨이다 — 이 fake는 config_json을 실제로 저장/조회하지
    # 않으므로 그 경로의 회귀를 잡을 수 없다. 진짜 게이트는
    # tests/store/test_analysis_store.py::test_run_라이프사이클과_결과_왕복
    # 의 `assert "config" not in latest` (config_json='{"k": 1}'로 실제
    # 저장 후 조회).
    latest = {"run_id": 7, "verdicts": [], "picks": []}
    resp = make_client(analysis=FakeAnalysis(latest=latest)).get(
        "/analyze/latest")
    assert "config" not in resp.json()
