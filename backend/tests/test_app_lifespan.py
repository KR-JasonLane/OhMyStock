from fastapi.testclient import TestClient

from app.adapters.kiwoom.broker import KiwoomBroker
from app.adapters.naver.client import NaverNewsClient
from app.core.config import Settings
from app.main import create_app


def _settings() -> Settings:
    return Settings(_env_file=None, kiwoom_app_key="AK", kiwoom_secret_key="SK",
                    kiwoom_mock=True, database_url="sqlite+pysqlite:///:memory:")


def _settings_with_naver_keys() -> Settings:
    return Settings(_env_file=None, kiwoom_app_key="AK", kiwoom_secret_key="SK",
                    kiwoom_mock=True, database_url="sqlite+pysqlite:///:memory:",
                    naver_client_id="test-id", naver_client_secret="test-secret")


def test_lifespan이_broker를_생성하고_보관한다():
    app = create_app(_settings())
    with TestClient(app):
        assert isinstance(app.state.broker, KiwoomBroker)


def test_lifespan_네이버_키가_없으면_news는_None이다():
    app = create_app(_settings())
    with TestClient(app):
        assert app.state.news is None


def test_lifespan_네이버_키가_있으면_NaverNewsClient를_만든다():
    app = create_app(_settings_with_naver_keys())
    with TestClient(app):
        assert isinstance(app.state.news, NaverNewsClient)
