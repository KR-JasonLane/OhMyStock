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

    @property
    def mode(self) -> str:
        return "mock" if self.kiwoom_mock else "real"


@lru_cache
def get_settings() -> Settings:
    return Settings()
