from fastapi.testclient import TestClient

from app.adapters.kiwoom.broker import KiwoomBroker
from app.core.config import Settings
from app.main import create_app


def _settings() -> Settings:
    return Settings(_env_file=None, kiwoom_app_key="AK", kiwoom_secret_key="SK",
                    kiwoom_mock=True, database_url="sqlite+pysqlite:///:memory:")


def test_lifespan이_broker를_생성하고_보관한다():
    app = create_app(_settings())
    with TestClient(app):
        assert isinstance(app.state.broker, KiwoomBroker)
