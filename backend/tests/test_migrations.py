from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_마이그레이션이_app_meta_테이블을_만든다(tmp_path, monkeypatch):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'mig.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))

    command.upgrade(cfg, "head")

    insp = inspect(create_engine(db_url))
    assert "app_meta" in insp.get_table_names()
    cols = {c["name"] for c in insp.get_columns("app_meta")}
    assert cols == {"key", "value"}


def test_현재_head는_0015이다(tmp_path, monkeypatch):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'head.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")
    with create_engine(db_url).connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version").scalar_one() == "0015"


def test_0013에서_0014로_기존_telegram_hash칼럼을_widen한다(
        tmp_path, monkeypatch):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'upgrade-0014.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))

    command.upgrade(cfg, "0013")
    before = inspect(create_engine(db_url))
    assert next(
        column["type"].length
        for column in before.get_columns("telegram_updates")
        if column["name"] == "operator_hash") == 64

    command.upgrade(cfg, "0014")
    after = inspect(create_engine(db_url))
    assert next(
        column["type"].length
        for column in after.get_columns("telegram_updates")
        if column["name"] == "operator_hash") == 67
    assert next(
        column["type"].length
        for column in after.get_columns("telegram_confirmations")
        if column["name"] == "chat_hash") == 67
