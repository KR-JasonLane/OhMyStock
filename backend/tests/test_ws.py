from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        kiwoom_app_key="k",
        kiwoom_secret_key="s",
        kiwoom_mock=True,
        database_url="sqlite+pysqlite:///:memory:",
    )


def test_ws_연결시_상태_프레임을_보낸다():
    app = create_app(_settings())
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            frame = ws.receive_json()
    assert frame == {"backend": "ok", "db": "ok", "mode": "mock"}
