from functools import lru_cache

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경변수 기반 설정. 필수값 누락 시 ValidationError로 즉시 실패(fail fast)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    kiwoom_app_key: SecretStr
    kiwoom_secret_key: SecretStr
    kiwoom_mock: bool = True
    database_url: SecretStr

    # 네이버 뉴스 검색 API 키 — 옵셔널. 키 미발급 상태에서도 기동은 정상이며,
    # 서비스는 키 부재 시 뉴스 조회를 생략하고 경고만 남긴다 (스펙 §4).
    naver_client_id: SecretStr | None = None
    naver_client_secret: SecretStr | None = None

    # 쓰기 엔드포인트(/collect,/score,/analyze) 보호용 API 키 — 옵셔널.
    # 미설정 시 차단하지 않고 기동 시 경고만 남긴다 (모의투자 로컬 개발
    # 편의, P3/P4 보안 패널 이월, 사용자 결정 2026-07-18 #24). Phase 5
    # 실전 전환 게이트에서 필수로 승격 예정.
    api_write_token: SecretStr | None = None

    # 주문 엔드포인트(/trade/start,/trade/stop) 전용 스코프 토큰(결정 #33 —
    # 조회/수집 트리거와 실주문 권한 분리). 미설정 시 api_write_token으로
    # 폴백(모의 편의). 실전 모드에서는 별도 설정 + write와 다른 값이 필수
    # (아래 validator — 스펙 §6-2-c).
    api_trade_token: SecretStr | None = None

    # 트레이딩 버그 봉쇄 한도(스펙 §8-1 — TradingConfig의 무기본값 4종).
    # **미설정 시 트레이딩 엔진 자체가 비활성**(하드 게이트: 상한 없이 실주문
    # 엔진이 켜지는 일이 없다 — main.py가 4개 전부 설정된 경우에만 조립).
    trade_max_single_order_krw: int | None = None
    trade_max_daily_orders: int | None = None
    trade_max_daily_order_krw: int | None = None
    trade_min_avg_trading_value_krw: int | None = None

    # CORS 허용 오리진 — 콤마 구분 문자열(리스트 필드 아님: pydantic-settings의
    # 리스트 타입 env 파싱은 JSON 문자열을 요구하는 함정이 있어 회피).
    # 기본값은 호스트 네이티브 Electron 렌더러의 로컬 dev 서버.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def mode(self) -> str:
        return "mock" if self.kiwoom_mock else "real"

    @model_validator(mode="after")
    def _실전_모드는_쓰기_토큰이_필수다(self) -> "Settings":
        # 왜: 모의투자에서는 미설정 시 경고만 남기고 쓰기 엔드포인트를 열어
        # 두지만(로컬 개발 편의), 실전 전환 순간부터는 인증 없는 매수/매도
        # 트리거가 실거래 사고로 직결된다. 결정 로그 #24("실전 전환 시
        # 필수 승격")를 코드로 강제해, 토큰 없이 kiwoom_mock=False로
        # 기동하는 것 자체를 fail-fast로 차단한다.
        if not self.kiwoom_mock and self.api_write_token is None:
            raise ValueError(
                "실전 모드(KIWOOM_MOCK=false)에서는 API_WRITE_TOKEN 설정이 "
                "필수입니다 — 인증 없는 쓰기 엔드포인트로 실거래를 트리거할 "
                "수 없습니다.")
        # 실전 스코프 토큰 강제(스펙 §6-2-c, v3 보안 #3): 주문 권한이 조회/
        # 수집 트리거와 같은 토큰이면 스코프 분리가 명목뿐이다 — 실전에서는
        # 별도 설정 + 서로 다른 값이 아니면 기동 자체를 차단(하드 게이트).
        # TRADE_* 한도는 all-or-nothing(아키텍트 P5-T7 #4 — 4종 중 일부만
        # 설정(오타 등)했는데 기동이 "성공"하고 트레이딩만 조용히 비활성이면
        # fail-fast 철학과 어긋난다). 하나라도 설정하면 전부 설정 강제.
        trade_limits = (self.trade_max_single_order_krw,
                        self.trade_max_daily_orders,
                        self.trade_max_daily_order_krw,
                        self.trade_min_avg_trading_value_krw)
        if any(v is not None for v in trade_limits) and \
                not all(v is not None for v in trade_limits):
            raise ValueError(
                "TRADE_* 한도는 전부 설정하거나 전부 비워야 합니다 — 일부만 "
                "설정된 상태는 오설정(오타)일 가능성이 높아 기동을 차단합니다"
                "(TRADE_MAX_SINGLE_ORDER_KRW/TRADE_MAX_DAILY_ORDERS/"
                "TRADE_MAX_DAILY_ORDER_KRW/TRADE_MIN_AVG_TRADING_VALUE_KRW).")
        if not self.kiwoom_mock:
            if self.api_trade_token is None:
                raise ValueError(
                    "실전 모드에서는 API_TRADE_TOKEN 설정이 필수입니다 — "
                    "주문 스코프를 쓰기 토큰과 분리해야 합니다(결정 #33).")
            if (self.api_write_token is not None
                    and self.api_trade_token.get_secret_value()
                    == self.api_write_token.get_secret_value()):
                raise ValueError(
                    "실전 모드에서는 API_TRADE_TOKEN이 API_WRITE_TOKEN과 "
                    "달라야 합니다 — 동일 값이면 스코프 분리가 명목뿐입니다.")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
