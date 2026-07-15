import pytest
from pydantic import ValidationError

from app.core.config import Settings

ENV = {
    "KIWOOM_APP_KEY": "test-key",
    "KIWOOM_SECRET_KEY": "test-secret",
    "KIWOOM_MOCK": "true",
    "DATABASE_URL": "sqlite+pysqlite:///:memory:",
}


def _set_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


def test_모든_환경변수를_로드한다(monkeypatch):
    _set_env(monkeypatch)
    s = Settings(_env_file=None)
    assert s.kiwoom_app_key == "test-key"
    assert s.kiwoom_secret_key == "test-secret"
    assert s.kiwoom_mock is True
    assert s.database_url == ENV["DATABASE_URL"]
    assert s.mode == "mock"


def test_필수_환경변수_누락시_즉시_실패한다(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.delenv("KIWOOM_APP_KEY")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_mock_false면_mode는_real(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("KIWOOM_MOCK", "false")
    assert Settings(_env_file=None).mode == "real"
