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
    # 실전 모드는 API_WRITE_TOKEN + API_TRADE_TOKEN(상이 값) 필수(스펙 §6-2-c,
    # P5-T7) — 이 테스트는 mode 프로퍼티만 확인하므로 더미로 통과시킨다.
    monkeypatch.setenv("API_WRITE_TOKEN", "dummy-token")
    monkeypatch.setenv("API_TRADE_TOKEN", "dummy-trade")
    assert Settings(_env_file=None).mode == "real"


def test_실전_모드에서_쓰기_토큰_없으면_즉시_실패한다(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("KIWOOM_MOCK", "false")
    monkeypatch.delenv("API_WRITE_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_실전_모드에서_두_토큰이_다르면_통과한다(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("KIWOOM_MOCK", "false")
    monkeypatch.setenv("API_WRITE_TOKEN", "real-token")
    monkeypatch.setenv("API_TRADE_TOKEN", "trade-token")
    s = Settings(_env_file=None)
    assert s.mode == "real"
    assert s.api_write_token.get_secret_value() == "real-token"
    assert s.api_trade_token.get_secret_value() == "trade-token"


def test_실전_모드에서_trade_토큰_없으면_즉시_실패한다(monkeypatch):
    # 스코프 분리 하드 게이트(§6-2-c — 결정 #33): 실전에서 주문 권한이
    # 쓰기 토큰에 묻어가면 안 된다
    _set_env(monkeypatch)
    monkeypatch.setenv("KIWOOM_MOCK", "false")
    monkeypatch.setenv("API_WRITE_TOKEN", "real-token")
    monkeypatch.delenv("API_TRADE_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_실전_모드에서_두_토큰이_같으면_즉시_실패한다(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("KIWOOM_MOCK", "false")
    monkeypatch.setenv("API_WRITE_TOKEN", "same-token")
    monkeypatch.setenv("API_TRADE_TOKEN", "same-token")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_TRADE_한도_일부만_설정하면_즉시_실패한다(monkeypatch):
    # all-or-nothing(P5-T7 아키텍트 #4) — 오타로 3/4만 설정됐는데 기동이
    # "성공"하고 트레이딩만 조용히 비활성이면 fail-fast 철학 위반
    _set_env(monkeypatch)
    monkeypatch.setenv("TRADE_MAX_SINGLE_ORDER_KRW", "1000000")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_TRADE_한도_전부_설정하면_통과한다(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("TRADE_MAX_SINGLE_ORDER_KRW", "1000000")
    monkeypatch.setenv("TRADE_MAX_DAILY_ORDERS", "50")
    monkeypatch.setenv("TRADE_MAX_DAILY_ORDER_KRW", "5000000")
    monkeypatch.setenv("TRADE_MIN_AVG_TRADING_VALUE_KRW", "0")
    s = Settings(_env_file=None)
    assert s.trade_max_daily_orders == 50


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


def test_빈_문자열_옵셔널은_None으로_정규화(monkeypatch):
    """실사고 회귀(2026-07-22 야간) — 빈 값("NAVER_CLIENT_ID=")이
    SecretStr("")로 살아남으면 `is not None` 분기가 빈 키로 네이버
    클라이언트를 조립해 뉴스 호출 전부 401. env_ignore_empty=True가
    빈 값=미설정을 전역 보장(공백만 있는 값은 한계 — config 주석)."""
    _set_env(monkeypatch)
    monkeypatch.setenv("NAVER_CLIENT_ID", "")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "")
    monkeypatch.setenv("API_WRITE_TOKEN", "")
    monkeypatch.setenv("KIWOOM_BASE_URL_OVERRIDE", "")
    s = Settings(_env_file=None)
    assert s.naver_client_id is None
    assert s.naver_client_secret is None
    assert s.api_write_token is None
    assert s.kiwoom_base_url_override is None
    assert s.run_environment == "mock"   # 빈 override는 리플레이 아님


def test_필수_시크릿의_빈_문자열도_즉시_실패(monkeypatch):
    """아키텍트 R-패치 — 빈 KIWOOM_APP_KEY=""가 SecretStr("")로 기동을
    통과하면 첫 실호출(빈 Bearer)에서야 터진다(옵셔널 사고와 동일 클래스).
    빈 값 → None → 필수 누락 ValidationError(fail-fast 계약 복원)."""
    _set_env(monkeypatch)
    monkeypatch.setenv("KIWOOM_APP_KEY", "")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    _set_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
