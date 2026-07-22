from fastapi.testclient import TestClient

from app.core.config import Settings
from app.domain.collection import CollectionProgress
from app.main import create_app


def _settings() -> Settings:
    return Settings(_env_file=None, kiwoom_app_key="AK", kiwoom_secret_key="SK",
                    kiwoom_mock=True, database_url="sqlite+pysqlite:///:memory:")


class StubService:
    """CollectionService의 start()/current_task()/progress() 계약을 흉내낸다.

    lifespan 종료 시 app.state.collection.current_task()가 호출되므로
    (미완료 태스크 취소) StubService도 이를 구현해야 TestClient 종료가
    깨지지 않는다 — 항상 완료된 것으로 취급해 None을 반환한다.
    """

    def __init__(self, running=False, progress=None):
        self._running = running
        self._progress = progress
        self.start_calls: list[str | None] = []

    def start(self, warning=None):
        self.start_calls.append(warning)
        if self._running:
            return None
        self._running = True
        return object()  # non-None sentinel — API만 None 여부를 확인한다

    def current_task(self):
        return None

    def progress(self):
        return self._progress


class FakeScoring:
    """scoring 실행 중 여부만 흉내낸다 — /collect의 대칭 409 가드 검증용.

    lifespan 종료 시 app.state.scoring.current_task()도 호출되므로
    (StubService의 동일 docstring 참고) 여기서도 구현해야 한다.
    """

    def __init__(self, running=False):
        self._running = running

    def is_running(self):
        return self._running

    def current_task(self):
        return None


def test_collect는_시작하면_202(monkeypatch):
    # 장 운영시간 경고는 실시간 is_market_hours()에 의존하므로 결정성을 위해 장외로
    # 고정(경고 없음). 장중/장외 경고 유무는 아래 test_장중이면.../test_장외면...이
    # 이미 엄격히 검증하므로 여기선 기본 202 스모크만 본다. monkeypatch 대상 지정은
    # 기존 관례(collect_mod)에 통일.
    import app.api.collect as collect_mod
    monkeypatch.setattr(collect_mod, "is_market_hours", lambda: False)
    app = create_app(_settings())
    with TestClient(app) as client:
        stub = StubService()
        app.state.collection = stub
        r = client.post("/collect")
    assert r.status_code == 202 and r.json() == {"started": True}


def test_이미_실행중이면_409():
    app = create_app(_settings())
    with TestClient(app) as client:
        app.state.collection = StubService(running=True)
        assert client.post("/collect").status_code == 409


def test_스코어링_실행중이면_409():
    app = create_app(_settings())
    with TestClient(app) as client:
        app.state.scoring = FakeScoring(running=True)
        app.state.collection = StubService()
        r = client.post("/collect")
    assert r.status_code == 409
    assert "scoring" in r.json()["detail"]


def test_status는_progress를_그대로_노출한다():
    app = create_app(_settings())
    with TestClient(app) as client:
        app.state.collection = StubService(progress=CollectionProgress(
            run_id=1, status="running", stage="candles", done=10, total=100, failed=2))
        body = client.get("/collect/status").json()
    assert body == {"run_id": 1, "status": "running", "stage": "candles",
                    "done": 10, "total": 100, "failed": 2}


def test_status는_warning이_있으면_함께_노출한다():
    app = create_app(_settings())
    with TestClient(app) as client:
        app.state.collection = StubService(progress=CollectionProgress(
            run_id=1, status="running", stage="candles", done=10, total=100, failed=2,
            warning="market-hours run may store unconfirmed candles"))
        body = client.get("/collect/status").json()
    assert body == {"run_id": 1, "status": "running", "stage": "candles",
                    "done": 10, "total": 100, "failed": 2,
                    "warning": "market-hours run may store unconfirmed candles"}


def test_최초에는_idle():
    app = create_app(_settings())
    with TestClient(app) as client:
        app.state.collection = StubService()
        assert client.get("/collect/status").json() == {"status": "idle"}


def test_장중이면_경고와_함께_202(monkeypatch):
    import app.api.collect as collect_mod
    monkeypatch.setattr(collect_mod, "is_market_hours", lambda: True)
    app = create_app(_settings())
    with TestClient(app) as client:
        stub = StubService()
        app.state.collection = stub
        r = client.post("/collect")
    assert r.status_code == 202
    assert r.json() == {"started": True,
                        "warning": "market-hours run may store unconfirmed candles"}
    assert stub.start_calls == ["market-hours run may store unconfirmed candles"]


def test_장외면_경고없이_202(monkeypatch):
    import app.api.collect as collect_mod
    monkeypatch.setattr(collect_mod, "is_market_hours", lambda: False)
    app = create_app(_settings())
    with TestClient(app) as client:
        stub = StubService()
        app.state.collection = stub
        r = client.post("/collect")
    assert r.status_code == 202
    assert r.json() == {"started": True}
    assert stub.start_calls == [None]
