from functools import lru_cache
from urllib.parse import urlparse

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 리플레이 프로필 override가 허용되는 호스트(정확 일치 — 스펙 §4-1 #1).
# 루프백 2종 + compose 컨테이너 네트워크의 replay 서비스명. 그 외 호스트는
# 기동 거부: 오타/오설정으로 앱키·시크릿이 임의 외부 호스트로 전송되는
# 자격증명 유출 경로를 사전 차단한다.
_OVERRIDE_ALLOWED_HOSTS = ("127.0.0.1", "localhost", "replay")


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

    # ── 리플레이 프로필(스펙 §4-1 — 자동 전환 없음, 명시적 env+재기동) ──
    # 설정되면 키움 어댑터가 mock/real URL 대신 이 값을 쓴다(로컬 리플레이
    # 목 서버 전용 — 아래 validator가 루프백/replay 서비스명 외 전부 거부).
    # 재생 기준 시각(앵커)은 별도 env가 아니라 **기동 시 /_replay/status
    # 프로브가 목 서버에서 취득**한다(main.py — 아키텍트/트레이더 R6:
    # 앵커 env 이중화(REPLAY_ANCHOR↔별도 앱 변수)의 값 드리프트와 서버·앱
    # 기동 시차 드리프트를 SSOT로 제거. 서버 미도달·speed≠1.0은 기동 거부).
    kiwoom_base_url_override: str | None = None

    @property
    def mode(self) -> str:
        return "mock" if self.kiwoom_mock else "real"

    @property
    def run_environment(self) -> str:
        """trade_runs.run_environment 감사 컬럼 값(§4-1 — 리플레이 런이
        P&L/리스크 집계를 오염하지 않도록 구조적 필터의 원천)."""
        if self.kiwoom_base_url_override is not None:
            return "replay"
        return self.mode

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

    @model_validator(mode="after")
    def _리플레이_override_조합_검증(self) -> "Settings":
        """스펙 §4-1(보안 패널 Critical #1 — 사후 감사가 아니라 사전 차단).
        ① override 호스트는 루프백/replay 서비스명 정확 일치만 허용(오타로
        실전 앱키·시크릿이 외부 호스트로 전송되는 유출 경로 차단).
        ② 실전 모드+override 조합은 기동 자체 차단(리플레이는 태생적으로
        mock 전제 — 실전 엔진이 목의 가짜 체결을 실체결로 오인해 실계좌
        포지션을 방치하는 역방향 리스크 봉쇄).
        (재생 앵커 정합·서버 도달성·speed는 env로 검증 불가 — 기동 시
        /_replay/status 프로브가 담당, main.py)"""
        if self.kiwoom_base_url_override is not None:
            parsed = urlparse(self.kiwoom_base_url_override)
            # 오류 메시지에 URL 원문을 echo하지 않는다(보안 R6 Minor —
            # 운영자가 user:password@host 형태로 넣은 경우 기동 실패
            # 로그에 인증정보가 남는 경로 차단). hostname/스킴만 노출.
            if parsed.scheme not in ("http", "https"):
                raise ValueError(
                    "KIWOOM_BASE_URL_OVERRIDE는 http(s) URL이어야 합니다 "
                    f"(수신 스킴: {parsed.scheme!r})")
            if parsed.hostname not in _OVERRIDE_ALLOWED_HOSTS:
                raise ValueError(
                    "KIWOOM_BASE_URL_OVERRIDE 호스트는 "
                    f"{_OVERRIDE_ALLOWED_HOSTS} 만 허용됩니다(리플레이 목 "
                    f"전용 — 임의 호스트는 자격증명 유출 경로): "
                    f"{parsed.hostname!r}")
            if not self.kiwoom_mock:
                raise ValueError(
                    "실전 모드(KIWOOM_MOCK=false)에서는 "
                    "KIWOOM_BASE_URL_OVERRIDE를 쓸 수 없습니다 — 리플레이 "
                    "프로필은 mock 전제입니다(가짜 체결을 실체결로 오인하는 "
                    "역방향 리스크).")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
