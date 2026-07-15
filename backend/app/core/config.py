from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경변수 기반 설정. 필수값 누락 시 ValidationError로 즉시 실패(fail fast)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    kiwoom_app_key: str
    kiwoom_secret_key: str
    kiwoom_mock: bool = True
    database_url: str

    @property
    def mode(self) -> str:
        return "mock" if self.kiwoom_mock else "real"


@lru_cache
def get_settings() -> Settings:
    return Settings()
