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
