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
    assert s.kiwoom_app_key.get_secret_value() == "test-key"
    assert s.kiwoom_secret_key.get_secret_value() == "test-secret"
    assert s.kiwoom_mock is True
    assert s.database_url.get_secret_value() == ENV["DATABASE_URL"]
    assert s.mode == "mock"


def test_시크릿은_repr에_노출되지_않는다(monkeypatch):
    _set_env(monkeypatch)
    s = Settings(_env_file=None)
    assert "test-key" not in repr(s)
    assert "test-key" not in str(s)


def test_필수_환경변수_누락시_즉시_실패한다(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.delenv("KIWOOM_APP_KEY")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_mock_false면_mode는_real(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("KIWOOM_MOCK", "false")
    # 실전 모드는 API_WRITE_TOKEN 필수(아래 검증기 테스트 참고) — 이 테스트는
    # mode 프로퍼티만 확인하므로 더미 토큰을 채워 검증기를 통과시킨다.
    monkeypatch.setenv("API_WRITE_TOKEN", "dummy-token")
    assert Settings(_env_file=None).mode == "real"


def test_실전_모드에서_쓰기_토큰_없으면_즉시_실패한다(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("KIWOOM_MOCK", "false")
    monkeypatch.delenv("API_WRITE_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_실전_모드에서_쓰기_토큰_있으면_통과한다(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("KIWOOM_MOCK", "false")
    monkeypatch.setenv("API_WRITE_TOKEN", "real-token")
    s = Settings(_env_file=None)
    assert s.mode == "real"
    assert s.api_write_token.get_secret_value() == "real-token"


def test_모의_모드에서는_쓰기_토큰_없어도_통과한다(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.delenv("API_WRITE_TOKEN", raising=False)
    s = Settings(_env_file=None)
    assert s.mode == "mock"
    assert s.api_write_token is None


def test_naver_키는_옵셔널(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.delenv("NAVER_CLIENT_ID", raising=False)
    monkeypatch.delenv("NAVER_CLIENT_SECRET", raising=False)
    s = Settings(_env_file=None)
    assert s.naver_client_id is None
    assert s.naver_client_secret is None


def test_naver_키가_있으면_로드된다(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("NAVER_CLIENT_ID", "nid")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "nsec")
    s = Settings(_env_file=None)
    assert s.naver_client_id.get_secret_value() == "nid"
    assert s.naver_client_secret.get_secret_value() == "nsec"
