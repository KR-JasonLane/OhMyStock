from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.domain.broker import Candle, Instrument, Sector
from app.store.collection_store import CollectionStore
from app.store.models import Base, InstrumentRow

NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)


def _store(tmp_path) -> CollectionStore:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    return CollectionStore(engine, now=lambda: NOW)


def _inst(symbol="005930", name="삼성전자") -> Instrument:
    return Instrument(symbol=symbol, name=name, market="kospi", instrument_type="보통주")


def test_instrument_upsert는_멱등이고_sector_code를_보존한다(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    s = CollectionStore(engine, now=lambda: NOW)

    s.upsert_sectors([Sector(code="001", market="kospi", name="전기전자")])
    s.upsert_instruments([_inst()])
    s.set_sector_codes({"005930": "001"})
    s.upsert_instruments([_inst(name="삼성전자(new)")])  # 재수집 — 이름 갱신
    assert s.list_symbols() == ["005930"]
    latest = s.latest_candle_date("005930")
    assert latest is None  # 봉은 아직 없음

    # sector_code가 upsert에 지워지지 않았는지: 직접 DB 조회로 검증
    with Session(engine) as session:
        sector_code = session.scalar(select(InstrumentRow.sector_code)
                                    .where(InstrumentRow.symbol == "005930"))
        assert sector_code == "001"


def test_candle_upsert는_멱등이다(tmp_path):
    s = _store(tmp_path)
    c = Candle(symbol="005930", date=date(2026, 7, 16), open=70000, high=71000,
               low=69900, close=70500, volume=1000)
    s.upsert_candles([c])
    s.upsert_candles([Candle(symbol="005930", date=date(2026, 7, 16), open=70000,
                             high=71000, low=69900, close=70600, volume=1100)])
    assert s.latest_candle_date("005930") == date(2026, 7, 16)


def test_run_라이프사이클(tmp_path):
    s = _store(tmp_path)
    run_id = s.create_run()
    assert isinstance(run_id, int)
    s.finish_run(run_id, "done", total=10, succeeded=9, failed=1)


def test_set_sector_codes는_미존재_심볼을_건너뛴다(tmp_path):
    """set_sector_codes skips unknown symbols and returns count of known."""
    s = _store(tmp_path)
    s.upsert_sectors([Sector(code="001", market="kospi", name="전기전자")])
    s.upsert_instruments([_inst(symbol="005930"), _inst(symbol="000660", name="SK하이닉스")])

    # Mapping includes unknown symbol "999999"
    result = s.set_sector_codes({"005930": "001", "999999": "001"})
    assert result == 1  # Only "005930" exists


def test_deactivate_missing은_활성_종목을_비활성화한다(tmp_path):
    """deactivate_missing marks inactive instruments not in seen_symbols."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    s = CollectionStore(engine, now=lambda: NOW)

    s.upsert_instruments([_inst(symbol="005930"), _inst(symbol="000660", name="SK하이닉스")])
    assert s.list_symbols() == ["000660", "005930"]

    # Deactivate all except "005930"
    result = s.deactivate_missing({"005930"})
    assert result == 1  # "000660" deactivated

    assert s.list_symbols() == ["005930"]
