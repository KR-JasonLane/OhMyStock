from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def _settings(database_url: str = "sqlite+pysqlite:///:memory:") -> Settings:
    return Settings(
        _env_file=None,
        kiwoom_app_key="k",
        kiwoom_secret_key="s",
        kiwoom_mock=True,
        database_url=database_url,
    )


def test_health_정상이면_ok(monkeypatch):
    app = create_app(_settings())
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok", "mode": "mock"}


def test_health_DB_다운이면_degraded(tmp_path):
    bad = tmp_path / "no" / "such" / "dir" / "x.db"
    app = create_app(_settings(f"sqlite+pysqlite:///{bad}"))
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "degraded", "db": "error", "mode": "mock"}
