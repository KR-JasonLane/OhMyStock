from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine

from app.domain.broker import Candle, Instrument, Sector
from app.domain.scoring.engine import (Candidate, ScoringResult, SectorScore,
                                       StrategyDetail)
from app.store.collection_store import CollectionStore
from app.store.models import Base
from app.store.scoring_store import ScoringStore

NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(eng)
    return eng


def test_run_라이프사이클과_결과_왕복(engine):
    store = ScoringStore(engine)
    run_id = store.create_run(reference_date=date(2026, 7, 17),
                              config_json='{"k": 1}')
    result = ScoringResult(
        sectors=(SectorScore("005", "음식료", 0.1, 0.2, 0.05, 1.0, 1, True),),
        candidates=(Candidate(
            symbol="AAA111", sector_code="005", rank=1, total_score=0.9,
            sector_score=1.0, strategy_score=0.8,
            details=(StrategyDetail("momentum", True, 0.05, 0.6, 4, 0.8),)),),
        excluded_short_history=2)
    store.save_results(run_id, result)
    store.finish_run(run_id, "succeeded", universe_count=100, stale_excluded=2)
    latest = store.latest_results()
    assert latest["run_id"] == run_id
    assert latest["candidates"][0]["symbol"] == "AAA111"
    assert latest["candidates"][0]["details"][0]["strategy"] == "momentum"
    assert latest["sectors"][0]["code"] == "005"


def test_latest는_succeeded만(engine):
    store = ScoringStore(engine)
    run_id = store.create_run(date(2026, 7, 17), "{}")
    store.finish_run(run_id, "failed", failure_reason="stale data")
    assert store.latest_results() is None


def test_universe_and_membership_queries(engine):
    # collection_store로 instruments/sectors/memberships/candles 셋업 후:
    # - active_common_instruments()는 kospi/kosdaq + is_active만 (etf 제외)
    # - industry_memberships()는 group_type='industry'만
    # - latest_dates/load_candles 왕복 (과거→최신 정렬 확인)
    collection = CollectionStore(engine, now=lambda: NOW)
    collection.upsert_sectors(
        [Sector("005", "kospi", "음식료/담배"),
         Sector("013", "kospi", "전기/전자"),
         Sector("001", "kospi", "종합(KOSPI)")],
        group_types={"005": "industry", "013": "industry", "001": "aggregate"})
    collection.upsert_instruments([
        Instrument("A0001", "가", "kospi", "보통주"),   # active kospi
        Instrument("A0002", "나", "kosdaq", "보통주"),  # active kosdaq
        Instrument("A0003", "다", "etf", "ETF"),        # active etf — 제외 대상
        Instrument("A0004", "라", "kospi", "보통주"),   # 비활성화 예정
    ])
    collection.deactivate_missing({"A0001", "A0002", "A0003"})  # A0004 비활성화

    collection.replace_sector_memberships({
        "005": ["A0001"],
        "013": ["A0002"],
        "001": ["A0001", "A0002"],  # aggregate — industry_memberships에서 제외돼야 함
    })

    collection.upsert_candles([
        Candle(symbol="A0001", date=date(2026, 7, 15), open=100, high=110,
               low=95, close=105, volume=1000),
        Candle(symbol="A0001", date=date(2026, 7, 16), open=105, high=115,
               low=100, close=110, volume=1200),
        Candle(symbol="A0002", date=date(2026, 7, 14), open=200, high=210,
               low=195, close=205, volume=500),
    ])

    store = ScoringStore(engine)

    active = store.active_common_instruments()
    active_symbols = {row[0] for row in active}
    assert active_symbols == {"A0001", "A0002"}  # etf(A0003)·비활성(A0004) 제외

    members, names = store.industry_memberships()
    assert set(members.keys()) == {"005", "013"}  # aggregate(001) 제외
    assert members["005"] == ["A0001"]
    assert members["013"] == ["A0002"]
    assert names == {"005": "음식료/담배", "013": "전기/전자"}

    latest_dates = store.latest_dates(["A0001", "A0002"])
    assert latest_dates == {"A0001": date(2026, 7, 16), "A0002": date(2026, 7, 14)}

    candles = store.load_candles(["A0001", "A0002"])
    assert [c.date for c in candles["A0001"]] == [date(2026, 7, 15), date(2026, 7, 16)]
    assert [c.close for c in candles["A0001"]] == [105, 110]  # 과거→최신 정렬
    assert [c.date for c in candles["A0002"]] == [date(2026, 7, 14)]
