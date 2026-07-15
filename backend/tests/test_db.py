from app.core.config import Settings
from app.store.db import check_db, create_db_engine


def _settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        kiwoom_app_key="k",
        kiwoom_secret_key="s",
        kiwoom_mock=True,
        database_url=database_url,
    )


def test_정상_DB면_check_db_True():
    engine = create_db_engine(_settings("sqlite+pysqlite:///:memory:"))
    assert check_db(engine) is True


def test_연결_불가_DB면_check_db_False(tmp_path):
    # 존재하지 않는 하위 디렉터리의 sqlite 파일 → 연결 시 OperationalError
    bad = tmp_path / "no" / "such" / "dir" / "x.db"
    engine = create_db_engine(_settings(f"sqlite+pysqlite:///{bad}"))
    assert check_db(engine) is False
