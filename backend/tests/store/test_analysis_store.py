from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine

from app.domain.analysis.graph import AnalysisResult, Pick
from app.domain.analysis.parsing import MarketContext, TraderVerdict
from app.domain.analysis.ports import Headline
from app.domain.broker import Instrument, Sector
from app.domain.scoring.engine import (Candidate, ScoringResult, SectorScore,
                                       StrategyDetail)
from app.store.analysis_store import AnalysisStore
from app.store.collection_store import CollectionStore
from app.store.models import Base
from app.store.scoring_store import ScoringStore

NOW = datetime(2026, 7, 18, 9, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(eng)
    return eng


def _seed_score_run(engine, reference_date: date = date(2026, 7, 17)) -> int:
    """CollectionStore로 instruments/sectors 시드 → ScoringStore로 succeeded
    스코어링 런(후보 2 + details)을 구성. AnalysisStore 테스트 공통 픽스처."""
    collection = CollectionStore(engine, now=lambda: NOW)
    collection.upsert_sectors(
        [Sector("005", "kospi", "음식료/담배"), Sector("013", "kospi", "전기/전자")],
        group_types={"005": "industry", "013": "industry"})
    collection.upsert_instruments([
        Instrument("AAA111", "가나다", "kospi", "보통주"),
        Instrument("BBB222", "라마바", "kospi", "보통주"),
    ])

    scoring = ScoringStore(engine, now=lambda: NOW)
    score_run_id = scoring.create_run(reference_date=reference_date, config_json="{}")
    result = ScoringResult(
        sectors=(
            SectorScore(code="005", name="음식료/담배", r20=0.02, r60=0.03, r5=0.01,
                       score=1.0, rank=1, selected=True),
            SectorScore(code="013", name="전기/전자", r20=-0.01, r60=0.01, r5=0.02,
                       score=0.5, rank=2, selected=True)),
        candidates=(
            Candidate(symbol="AAA111", sector_code="005", rank=1, total_score=0.9,
                     sector_score=1.0, strategy_score=0.8, strategy_score_norm=0.8,
                     details=(StrategyDetail("momentum", True, 0.05, 0.6, 4, 0.8),)),
            Candidate(symbol="BBB222", sector_code="013", rank=2, total_score=0.5,
                     sector_score=0.5, strategy_score=0.4, strategy_score_norm=0.4,
                     details=(StrategyDetail("breakout", False, 0.01, 0.4, 2, 0.4),))),
        excluded_short_history=0)
    scoring.save_results(score_run_id, result)
    scoring.finish_run(score_run_id, "succeeded", universe_count=2, stale_excluded=0)
    return score_run_id


def test_latest_succeeded_score_run_없으면_None(engine):
    store = AnalysisStore(engine)
    assert store.latest_succeeded_score_run() is None

    # running/failed 런은 카운트되지 않는다
    scoring = ScoringStore(engine, now=lambda: NOW)
    run_id = scoring.create_run(reference_date=date(2026, 7, 17), config_json="{}")
    scoring.finish_run(run_id, "failed", failure_reason="stale data")
    assert store.latest_succeeded_score_run() is None


def test_latest_succeeded_score_run_있으면_반환(engine):
    score_run_id = _seed_score_run(engine, reference_date=date(2026, 7, 17))
    store = AnalysisStore(engine)
    assert store.latest_succeeded_score_run() == (score_run_id, date(2026, 7, 17))


def test_load_candidates_조인과_순서(engine):
    score_run_id = _seed_score_run(engine)
    store = AnalysisStore(engine)
    cands = store.load_candidates(score_run_id)

    assert [c.symbol for c in cands] == ["AAA111", "BBB222"]  # rank 순
    assert cands[0].name == "가나다"
    assert cands[0].sector_name == "음식료/담배"
    assert cands[0].details[0].strategy == "momentum"
    assert cands[0].details[0].occurrences == 4
    assert cands[1].sector_name == "전기/전자"


def test_market_snapshot_표와_breadth(engine):
    collection = CollectionStore(engine, now=lambda: NOW)
    collection.upsert_sectors(
        [Sector("005", "kospi", "음식료/담배"), Sector("013", "kospi", "전기/전자"),
         Sector("020", "kospi", "화학")],
        group_types={"005": "industry", "013": "industry", "020": "industry"})

    scoring = ScoringStore(engine, now=lambda: NOW)
    score_run_id = scoring.create_run(reference_date=date(2026, 7, 17), config_json="{}")
    result = ScoringResult(
        sectors=(
            SectorScore(code="005", name="음식료/담배", r20=0.02, r60=0.03, r5=0.01,
                       score=1.0, rank=1, selected=True),   # r20 > 0
            SectorScore(code="013", name="전기/전자", r20=0.05, r60=-0.01, r5=0.02,
                       score=0.5, rank=2, selected=True),   # r20 > 0
            SectorScore(code="020", name="화학", r20=-0.02, r60=-0.03, r5=-0.01,
                       score=0.1, rank=3, selected=False)),  # r20 <= 0
        candidates=(), excluded_short_history=0)
    scoring.save_results(score_run_id, result)
    scoring.finish_run(score_run_id, "succeeded")

    store = AnalysisStore(engine)
    snapshot = store.market_snapshot(score_run_id)

    assert snapshot.breadth == pytest.approx(2 / 3)
    lines = snapshot.sector_table.splitlines()
    assert len(lines) == 3
    assert "음식료/담배" in snapshot.sector_table
    assert "전기/전자" in snapshot.sector_table
    assert "화학" in snapshot.sector_table
    # 업종명 오름차순 정렬 (음식료/담배 < 전기/전자 < 화학, 코드포인트 순)
    assert lines == sorted(lines)


def test_market_snapshot_행_0개면_breadth_0(engine):
    scoring = ScoringStore(engine, now=lambda: NOW)
    score_run_id = scoring.create_run(reference_date=date(2026, 7, 17), config_json="{}")
    scoring.save_results(score_run_id, ScoringResult(
        sectors=(), candidates=(), excluded_short_history=0))
    scoring.finish_run(score_run_id, "succeeded")

    store = AnalysisStore(engine)
    snapshot = store.market_snapshot(score_run_id)
    assert snapshot.breadth == 0.0
    assert snapshot.sector_table == ""


def test_run_라이프사이클과_결과_왕복(engine):
    score_run_id = _seed_score_run(engine)
    store = AnalysisStore(engine)
    run_id = store.create_run(score_run_id, model="gpt-x", prompt_hash="abc123def456",
                              config_json='{"k": 1}')

    verdicts = {
        "AAA111": TraderVerdict(verdict="approve", confidence=0.9,
                               reasons=("전략 신호 양호",), risk_flags=()),
        "BBB222": TraderVerdict(verdict="approve", confidence=0.6,
                               reasons=("업종 강세",), risk_flags=("표본 부족",)),
        "CCC333": TraderVerdict(verdict="reject", confidence=0.2,
                               reasons=("확신 부족",), risk_flags=()),
    }
    picks = (Pick(symbol="AAA111", rank=1),)
    market = MarketContext(regime="neutral", summary="관망 우세", max_picks_advice=1,
                           cautions=())
    result = AnalysisResult(market=market, verdicts=verdicts, picks=picks,
                           warnings=("w1", "w2"))
    news = {
        "market": [Headline(title="시장 뉴스", url="http://news.example/m1",
                           published_at="2026-07-18")],
        "AAA111": [Headline(title="종목 뉴스", url="http://news.example/a1",
                           published_at="2026-07-18")],
    }
    store.save_results(run_id, result, news)
    store.finish_run(run_id, "succeeded", regime="neutral", market_summary="관망 우세",
                     warnings="w1; w2")

    latest = store.latest_results()
    assert latest["run_id"] == run_id
    assert latest["score_run_id"] == score_run_id
    assert latest["model"] == "gpt-x"
    assert latest["prompt_hash"] == "abc123def456"
    assert latest["regime"] == "neutral"
    assert latest["market_summary"] == "관망 우세"
    assert latest["warnings"] == "w1; w2"

    # picks는 pick_rank 순, 승인된 2건 중 실제 선정된 1건만
    assert [p["symbol"] for p in latest["picks"]] == ["AAA111"]

    # verdicts는 전건(승인 2 + 거부 1)
    assert {v["symbol"] for v in latest["verdicts"]} == {"AAA111", "BBB222", "CCC333"}
    by_symbol = {v["symbol"]: v for v in latest["verdicts"]}
    assert by_symbol["AAA111"]["picked"] is True
    assert by_symbol["AAA111"]["pick_rank"] == 1
    assert by_symbol["AAA111"]["reasons"] == ["전략 신호 양호"]  # 리스트로 복원
    assert by_symbol["BBB222"]["picked"] is False  # 승인이지만 선정 안 됨
    assert by_symbol["BBB222"]["risk_flags"] == ["표본 부족"]
    assert by_symbol["CCC333"]["verdict"] == "reject"
    assert by_symbol["CCC333"]["reasons"] == ["확신 부족"]

    # 뉴스 스냅샷 자체는 포함하지 않고 개수만 (market 1 + 종목 1)
    assert "news" not in latest
    assert latest["news_count"] == 2


def test_latest는_succeeded만(engine):
    score_run_id = _seed_score_run(engine)
    store = AnalysisStore(engine)
    run_id = store.create_run(score_run_id, "gpt-x", "abc123def456", "{}")
    store.finish_run(run_id, "failed", failure_reason="llm-connect-error")
    assert store.latest_results() is None
