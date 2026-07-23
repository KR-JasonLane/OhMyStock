from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

BACKEND_DIR = Path(__file__).resolve().parents[2]


def test_0002가_시장데이터_테이블_4종을_만든다(tmp_path, monkeypatch):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'mig.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")
    names = set(inspect(create_engine(db_url)).get_table_names())
    assert {"sectors", "instruments", "candles", "collection_runs"} <= names


def test_0003이_섹터_멤버십_테이블과_instrument_상태_칼럼을_추가한다(tmp_path, monkeypatch):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'mig.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")

    insp = inspect(create_engine(db_url))
    assert "sector_memberships" in set(insp.get_table_names())

    sector_cols = {c["name"] for c in insp.get_columns("sectors")}
    assert "group_type" in sector_cols

    instrument_cols = {c["name"] for c in insp.get_columns("instruments")}
    assert {"state", "audit_info"} <= instrument_cols
    assert "sector_code" not in instrument_cols


def test_0003_downgrade_후_다시_upgrade해도_성공한다(tmp_path, monkeypatch):
    """0003 downgrade가 instruments 칼럼 add/drop을 모두 batch_alter_table로
    묶었는지 회귀 검증 — 섞이면 sqlite recreate 경로에서 깨진다."""
    db_url = f"sqlite+pysqlite:///{tmp_path / 'mig.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0002")

    insp = inspect(create_engine(db_url))
    assert "sector_memberships" not in set(insp.get_table_names())

    instrument_cols = {c["name"] for c in insp.get_columns("instruments")}
    assert "sector_code" in instrument_cols
    assert not {"state", "audit_info"} & instrument_cols

    sector_cols = {c["name"] for c in insp.get_columns("sectors")}
    assert "group_type" not in sector_cols

    command.upgrade(cfg, "head")
    insp = inspect(create_engine(db_url))
    assert "sector_memberships" in set(insp.get_table_names())


def test_0004가_스코어링_결과_테이블_4종을_만든다(tmp_path, monkeypatch):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'mig.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")
    names = set(inspect(create_engine(db_url)).get_table_names())
    assert {"score_runs", "score_sectors", "scores", "score_details"} <= names


def test_0004_downgrade_후_다시_upgrade해도_성공한다(tmp_path, monkeypatch):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'mig.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0003")
    insp = inspect(create_engine(db_url))
    names = set(insp.get_table_names())
    assert not {"score_runs", "score_sectors", "scores", "score_details"} & names

    command.upgrade(cfg, "head")
    insp = inspect(create_engine(db_url))
    names = set(insp.get_table_names())
    assert {"score_runs", "score_sectors", "scores", "score_details"} <= names


def test_0004의_scores는_strategy_score_norm_칼럼을_가진다(tmp_path, monkeypatch):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'mig.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")
    insp = inspect(create_engine(db_url))
    score_cols = {c["name"] for c in insp.get_columns("scores")}
    assert "strategy_score_norm" in score_cols


def test_0004의_run_id_외래키_3종은_CASCADE_삭제다(tmp_path, monkeypatch):
    """retention 삭제(오래된 run 정리)가 child 테이블을 먼저 지우는 수동
    순서 없이도 동작해야 한다 — 아키텍처 패널 지적."""
    db_url = f"sqlite+pysqlite:///{tmp_path / 'mig.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")
    insp = inspect(create_engine(db_url))
    for table in ("score_sectors", "scores", "score_details"):
        fks = insp.get_foreign_keys(table)
        run_id_fk = next(fk for fk in fks if fk["constrained_columns"] == ["run_id"])
        assert run_id_fk["options"].get("ondelete") == "CASCADE", table


def test_0005가_분석_결과_테이블_3종을_만든다(tmp_path, monkeypatch):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'mig.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")
    names = set(inspect(create_engine(db_url)).get_table_names())
    assert {"analysis_runs", "analysis_verdicts", "analysis_news"} <= names


def test_0005_downgrade_후_다시_upgrade해도_성공한다(tmp_path, monkeypatch):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'mig.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0004")
    insp = inspect(create_engine(db_url))
    names = set(insp.get_table_names())
    assert not {"analysis_runs", "analysis_verdicts", "analysis_news"} & names

    command.upgrade(cfg, "head")
    insp = inspect(create_engine(db_url))
    names = set(insp.get_table_names())
    assert {"analysis_runs", "analysis_verdicts", "analysis_news"} <= names


def test_0005의_run_id_외래키_2종은_CASCADE_삭제다(tmp_path, monkeypatch):
    """analysis_runs 삭제(retention 정리) 시 verdicts/news가 수동 순서 없이도
    함께 정리돼야 한다 — 0004의 CASCADE 패턴과 동일."""
    db_url = f"sqlite+pysqlite:///{tmp_path / 'mig.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")
    insp = inspect(create_engine(db_url))
    for table in ("analysis_verdicts", "analysis_news"):
        fks = insp.get_foreign_keys(table)
        run_id_fk = next(fk for fk in fks if fk["constrained_columns"] == ["run_id"])
        assert run_id_fk["options"].get("ondelete") == "CASCADE", table


def test_0007이_트레이딩_테이블_4종을_만들고_0008이_폴백_칼럼을_추가한다(
        tmp_path, monkeypatch):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'mig.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")
    insp = inspect(create_engine(db_url))
    names = set(insp.get_table_names())
    assert {"trade_runs", "trade_positions", "trade_orders", "trade_fills"} <= names
    # 0008 — analysis_runs.economist_fallback (별도 리비전, 규칙 4)
    cols = {c["name"] for c in insp.get_columns("analysis_runs")}
    assert "economist_fallback" in cols

    # downgrade 왕복 — 0006으로 되돌리면 트레이딩 테이블·폴백 칼럼 제거
    command.downgrade(cfg, "0006")
    insp = inspect(create_engine(db_url))
    names = set(insp.get_table_names())
    assert not {"trade_runs", "trade_positions", "trade_orders", "trade_fills"} & names
    cols = {c["name"] for c in insp.get_columns("analysis_runs")}
    assert "economist_fallback" not in cols


def test_0007_트레이딩_FK는_전부_비CASCADE다(tmp_path, monkeypatch):
    """실거래 감사 자산은 연쇄 삭제 금지(스펙 §9 RESTRICT) —
    AnalysisRunRow.score_run_id의 non-CASCADE 관례와 정합."""
    db_url = f"sqlite+pysqlite:///{tmp_path / 'mig.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")
    insp = inspect(create_engine(db_url))
    for table in ("trade_positions", "trade_orders", "trade_fills"):
        for fk in insp.get_foreign_keys(table):
            assert fk["options"].get("ondelete") is None, (table, fk)


def test_0010_0011이_est_krw_인덱스_스케줄러_이벤트를_만든다(
        tmp_path, monkeypatch):
    """P6 Task 1(0010: trade_orders.est_krw + 복합 인덱스) + Task 4(0011:
    scheduler_events 5컬럼)의 실제 마이그레이션 SQL 왕복(개발자 T4 —
    create_all 우회 검증은 마이그레이션 자체를 증명하지 못한다)."""
    db_url = f"sqlite+pysqlite:///{tmp_path / 'mig.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")
    insp = inspect(create_engine(db_url))
    cols = {c["name"] for c in insp.get_columns("trade_orders")}
    assert "est_krw" in cols
    index_names = {ix["name"] for ix in insp.get_indexes("trade_orders")}
    assert "ix_trade_orders_run_created" in index_names
    sched_cols = {c["name"] for c in insp.get_columns("scheduler_events")}
    assert sched_cols == {"id", "ts", "job", "action", "reason", "run_id"}
    fks = insp.get_foreign_keys("scheduler_events")
    assert fks == []          # 폴리모픽 run_id — FK 미설정이 의도

    command.downgrade(cfg, "0009")
    insp = inspect(create_engine(db_url))
    assert "scheduler_events" not in set(insp.get_table_names())
    cols = {c["name"] for c in insp.get_columns("trade_orders")}
    assert "est_krw" not in cols
