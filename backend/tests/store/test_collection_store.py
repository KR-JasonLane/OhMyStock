from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.domain.broker import Candle, Instrument, Sector
from app.store.collection_store import CollectionStore
from app.store.models import Base, InstrumentRow, SectorMembershipRow, SectorRow

NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)


def _store(tmp_path) -> CollectionStore:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    return CollectionStore(engine, now=lambda: NOW)


def _inst(symbol="005930", name="삼성전자") -> Instrument:
    return Instrument(symbol=symbol, name=name, market="kospi", instrument_type="보통주")


def test_instrument_upsert는_멱등이다(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    s = CollectionStore(engine, now=lambda: NOW)

    s.upsert_sectors([Sector(code="001", market="kospi", name="전기전자")],
                     group_types={"001": "industry"})
    s.upsert_instruments([_inst()])
    s.upsert_instruments([_inst(name="삼성전자(new)")])  # 재수집 — 이름 갱신
    assert s.list_symbols() == ["005930"]
    latest = s.latest_candle_date("005930")
    assert latest is None  # 봉은 아직 없음

    # upsert가 기존 행을 새 값으로 갱신했는지: 직접 DB 조회로 검증
    with Session(engine) as session:
        name = session.scalar(select(InstrumentRow.name)
                              .where(InstrumentRow.symbol == "005930"))
        assert name == "삼성전자(new)"


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


def test_set_sector_codes는_deprecated_no_op이다(tmp_path):
    """set_sector_codes: sector_code 칼럼이 0003에서 삭제되어 더 이상 반영할 곳이
    없다. Task 3에서 domain/collection.py의 호출부를 replace_sector_memberships로
    전환하며 이 메서드도 제거될 때까지, 시그니처만 유지한 채 항상 0을 반환한다."""
    s = _store(tmp_path)
    s.upsert_sectors([Sector(code="001", market="kospi", name="전기전자")],
                     group_types={"001": "industry"})
    s.upsert_instruments([_inst(symbol="005930"), _inst(symbol="000660", name="SK하이닉스")])

    result = s.set_sector_codes({"005930": "001", "999999": "001"})
    assert result == 0


def test_멤버십_전체_교체(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    s = CollectionStore(engine, now=lambda: NOW)
    s.upsert_sectors([Sector("005", "kospi", "음식료/담배"),
                      Sector("013", "kospi", "전기/전자")],
                     group_types={"005": "industry", "013": "industry"})
    s.upsert_instruments([
        Instrument("A0001", "가", "kospi", "A", state="증거금100%",
                  audit_info="정상"),
        Instrument("A0002", "나", "kospi", "A")])

    n = s.replace_sector_memberships(
        {"005": ["A0001"], "013": ["A0001", "A0002", "ZZZZ9"]})
    assert n == 3  # ZZZZ9는 미등록 → 스킵

    # 재호출은 이전 소속을 남기지 않는다 (전체 교체)
    n2 = s.replace_sector_memberships({"005": ["A0002"]})
    assert n2 == 1
    with Session(engine) as session:
        rows = session.execute(select(SectorMembershipRow)).scalars().all()
        assert [(r.sector_code, r.symbol) for r in rows] == [("005", "A0002")]


def test_instrument_상태_저장(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    s = CollectionStore(engine, now=lambda: NOW)
    s.upsert_instruments([Instrument("A0001", "가", "kospi", "A",
                                     state="관리종목", audit_info="관리종목")])
    with Session(engine) as session:
        row = session.get(InstrumentRow, "A0001")
        assert row.state == "관리종목" and row.audit_info == "관리종목"


def test_sectors_group_type_저장(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    s = CollectionStore(engine, now=lambda: NOW)
    s.upsert_sectors([Sector("001", "kospi", "종합(KOSPI)")],
                     group_types={"001": "aggregate"})
    with Session(engine) as session:
        assert session.get(SectorRow, "001").group_type == "aggregate"


def test_latest_candle_dates는_전_종목_최신일자를_일괄_반환한다(tmp_path):
    """latest_candle_dates: 종목별 반복 호출 없이 단일 쿼리로 최신일자 dict를 얻는다."""
    s = _store(tmp_path)
    s.upsert_candles([
        Candle(symbol="005930", date=date(2026, 7, 15), open=1, high=2, low=1,
               close=2, volume=1),
        Candle(symbol="005930", date=date(2026, 7, 16), open=1, high=2, low=1,
               close=2, volume=1),
        Candle(symbol="000660", date=date(2026, 7, 14), open=1, high=2, low=1,
               close=2, volume=1),
    ])
    assert s.latest_candle_dates() == {
        "005930": date(2026, 7, 16),
        "000660": date(2026, 7, 14),
    }


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
