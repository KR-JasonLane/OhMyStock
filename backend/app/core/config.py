from functools import lru_cache

from pydantic import SecretStr
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

    # CORS 허용 오리진 — 콤마 구분 문자열(리스트 필드 아님: pydantic-settings의
    # 리스트 타입 env 파싱은 JSON 문자열을 요구하는 함정이 있어 회피).
    # 기본값은 호스트 네이티브 Electron 렌더러의 로컬 dev 서버.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def mode(self) -> str:
        return "mock" if self.kiwoom_mock else "real"


@lru_cache
def get_settings() -> Settings:
    return Settings()
