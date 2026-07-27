"""Lifespan composition contracts added after the general app tests."""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.core.config import Settings
from app.store.models import Base


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "kiwoom_app_key": "AK",
        "kiwoom_secret_key": "SK",
        "kiwoom_mock": True,
        "database_url": f"sqlite+pysqlite:///{tmp_path / 'main.db'}",
        "scheduler_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_telegram_lifespan은_분석요약_read_store와_service에_같은환경을전달한다(
        tmp_path, monkeypatch):
    from app import main

    settings = _settings(
        tmp_path,
        telegram_bot_token="BOT-TOKEN",
        telegram_allowed_user_id=11,
        telegram_allowed_chat_id=22,
    )
    engine = create_engine(settings.database_url.get_secret_value())
    Base.metadata.create_all(engine)
    engine.dispose()
    captured = {}

    class NoNetworkTelegram:
        def __init__(self, _token) -> None:
            self.closed = False

        async def get_updates(self, _offset):
            return []

        async def send_message(self, _chat_id, _text):
            raise AssertionError("lifespan assembly must not send Telegram messages")

        async def aclose(self):
            self.closed = True

    class CapturingRuns:
        def __init__(self, _engine, run_environment) -> None:
            captured["runs_environment"] = run_environment

    class CapturingSummaryService:
        def __init__(self, runs, _store, *, run_environment) -> None:
            captured["runs"] = runs
            captured["service_environment"] = run_environment

        async def run_once(self):
            return 0

        def snapshot(self):
            return {
                "state": "running", "last_created": 0,
                "backoff_reason": None,
            }

    monkeypatch.setattr(main, "TelegramClient", NoNetworkTelegram)
    monkeypatch.setattr(main, "AnalysisSummaryRunStore", CapturingRuns)
    monkeypatch.setattr(main, "AnalysisSummaryService", CapturingSummaryService)
    app = main.create_app(settings)

    with TestClient(app):
        snapshot = app.state.telegram_service.snapshot()
        assert captured["runs_environment"] == "mock"
        assert captured["service_environment"] == "mock"
        assert isinstance(captured["runs"], CapturingRuns)
        assert snapshot["analysis_summary"] == {
            "state": "running", "last_created": 0, "backoff_reason": None,
        }
