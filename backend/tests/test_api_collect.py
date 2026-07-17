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


def test_collect는_시작하면_202():
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
