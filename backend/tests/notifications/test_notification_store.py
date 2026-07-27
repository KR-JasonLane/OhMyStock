import json
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.domain.notifications.analysis_summary import MorningAnalysisSummary
from app.domain.notifications.models import NotificationPriority
from app.store.models import (AnalysisRunRow, AnalysisVerdictRow, Base,
                              InstrumentRow, NotificationOutboxRow, ScoreRunRow)
from app.store.notification_store import AnalysisSummaryRunStore, NotificationStore


KST = __import__("zoneinfo").ZoneInfo("Asia/Seoul")


def kst(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=KST)


def utc(value):
    return value.astimezone(timezone.utc)


def _add_score(session: Session, reference_date: date) -> int:
    row = ScoreRunRow(
        started_at=utc(kst(2026, 7, 27, 0, 10)),
        finished_at=utc(kst(2026, 7, 27, 0, 11)),
        status="succeeded", reference_date=reference_date, universe_count=3,
        stale_excluded=0, failure_reason=None, config="{}",
    )
    session.add(row)
    session.flush()
    return row.id


def _add_analysis(
        session: Session, run_id: int, score_run_id: int, *, started_at: datetime,
        status: str = "succeeded", finished_at: datetime | None = None,
        completed: bool = True, regime: str = "neutral",
        max_picks_advice: int = 1) -> None:
    session.add(AnalysisRunRow(
        id=run_id, started_at=utc(started_at),
        finished_at=(utc(finished_at or (started_at + timedelta(minutes=1)))
                     if completed else None),
        status=status, score_run_id=score_run_id, model="test", prompt_hash="test",
        config="{}", regime=regime, market_summary="시장 요약", warnings=None,
        failure_reason=None, max_picks_advice=max_picks_advice,
        economist_fallback=False,
    ))


def _add_verdict(
        session: Session, run_id: int, symbol: str, *, verdict: str = "approve",
        confidence: float = 0.8, reasons: str = '["근거"]',
        risk_flags: str = '["위험"]', picked: bool = False,
        pick_rank: int | None = None) -> None:
    session.add(AnalysisVerdictRow(
        run_id=run_id, symbol=symbol, verdict=verdict, confidence=confidence,
        reasons=reasons, risk_flags=risk_flags, picked=picked, pick_rank=pick_rank,
    ))


def test_pending_succeeded_today는_당일성공런만_생성이력제외후_오래된순으로_읽는다(
        tmp_path):
    """status/일자/이미 materialize한 run을 빼는 조회 분기가 깨지면 실패한다."""
    now = kst(2026, 7, 27, 9)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'analysis.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        score_id = _add_score(session, date(2026, 7, 24))
        session.add(InstrumentRow(
            symbol="000004", name="넷째", market="kospi", instrument_type="",
            state="", audit_info="", is_active=True, updated_at=utc(now)))
        _add_analysis(session, 1, score_id, started_at=kst(2026, 7, 26, 8, 20))
        _add_analysis(session, 2, score_id, started_at=kst(2026, 7, 27, 8, 10))
        _add_analysis(session, 3, score_id, started_at=kst(2026, 7, 27, 8, 15),
                      status="failed")
        _add_analysis(session, 4, score_id, started_at=kst(2026, 7, 27, 8, 20))
        _add_analysis(session, 5, score_id, started_at=kst(2026, 7, 27, 8, 25))
        _add_analysis(session, 6, score_id, started_at=kst(2026, 7, 27, 8, 30),
                      completed=False)
        _add_verdict(session, 4, "000004", picked=True, pick_rank=1)
        _add_verdict(session, 5, "000005", verdict="reject")
        session.commit()

    store = AnalysisSummaryRunStore(engine, "mock", now=lambda: now)

    assert tuple(item.run_id for item in store.pending_succeeded_today({2}, limit=1)) == (4,)
    summaries = store.pending_succeeded_today({2})
    assert tuple(item.run_id for item in summaries) == (4, 5)
    assert summaries[0].score_reference_date == date(2026, 7, 24)
    assert summaries[0].verdicts[0].name == "넷째"
    assert store.latest_analysis().run_id == 5


def test_read_model은_손상된_이유위험JSON을_빈값으로격리하고_행수를_표시한다(tmp_path):
    """JSON 형식 오류가 전체 자동 알림을 중단시키는 회귀를 막는다."""
    now = kst(2026, 7, 27, 9)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'corrupt-json.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        score_id = _add_score(session, date(2026, 7, 24))
        _add_analysis(session, 7, score_id, started_at=kst(2026, 7, 27, 8, 20))
        _add_verdict(session, 7, "000007", reasons='{"not": "an array"}',
                     risk_flags="[1]", picked=True, pick_rank=1)
        session.commit()

    summary = AnalysisSummaryRunStore(engine, "mock", now=lambda: now).latest_analysis()

    assert summary is not None
    assert summary.verdicts[0].reasons == ()
    assert summary.verdicts[0].risk_flags == ()
    assert summary.corrupted_rows == 1


def test_read_model은_SQLite_Text의_invalid_utf8_JSON을_빈값으로격리한다(tmp_path):
    """SQLite Text에 든 잘못된 UTF-8 byte가 JSON loader 예외를 내면 실패한다."""
    now = kst(2026, 7, 27, 9)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'invalid-utf8.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        score_id = _add_score(session, date(2026, 7, 24))
        _add_analysis(session, 7, score_id, started_at=kst(2026, 7, 27, 8, 20))
        _add_verdict(session, 7, "000007", picked=True, pick_rank=1)
        session.flush()
        session.execute(text("""
            UPDATE analysis_verdicts
            SET reasons = :invalid_utf8
            WHERE run_id = :run_id AND symbol = :symbol
        """), {"invalid_utf8": b"\xff", "run_id": 7, "symbol": "000007"})
        session.commit()

    summary = AnalysisSummaryRunStore(engine, "mock", now=lambda: now).latest_analysis()

    assert summary is not None
    assert summary.verdicts[0].reasons == ()
    assert summary.verdicts[0].risk_flags == ("위험",)
    assert summary.corrupted_rows == 1


def test_read_model은_불연속후보순위를_최종후보에서_제외하고_손상을_표시한다(tmp_path):
    """손상된 순위가 순위를 재해석해 최종 후보를 만들어 내면 실패한다."""
    now = kst(2026, 7, 27, 9)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'corrupt-rank.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        score_id = _add_score(session, date(2026, 7, 24))
        _add_analysis(session, 7, score_id, started_at=kst(2026, 7, 27, 8, 20),
                      max_picks_advice=2)
        _add_verdict(session, 7, "000001", picked=True, pick_rank=1)
        _add_verdict(session, 7, "000003", picked=True, pick_rank=3)
        _add_verdict(session, 7, "000004", verdict="reject")
        session.commit()

    summary = AnalysisSummaryRunStore(engine, "mock", now=lambda: now).latest_analysis()

    assert summary is not None
    assert tuple(item.symbol for item in summary.verdicts) == ("000004",)
    assert summary.corrupted_rows == 2


def test_read_model은_20건초과_verdict_전체를_격리하고_손상을_표시한다(tmp_path):
    """20건 초과에서 임의의 선두 행을 정답처럼 보이면 실패한다."""
    now = kst(2026, 7, 27, 9)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'corrupt-count.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        score_id = _add_score(session, date(2026, 7, 24))
        _add_analysis(session, 7, score_id, started_at=kst(2026, 7, 27, 8, 20))
        for number in range(21):
            _add_verdict(session, 7, f"{number:06d}", verdict="reject")
        session.commit()

    summary = AnalysisSummaryRunStore(engine, "mock", now=lambda: now).latest_analysis()

    assert summary is not None
    assert summary.verdicts == ()
    assert summary.corrupted_rows == 21


def test_latest_analysis는_run_id가아닌_가장늦은_완료시각을_읽는다(tmp_path):
    """느린 먼저 시작한 분석을 이전 완료 run으로 오인하면 실패한다."""
    now = kst(2026, 7, 27, 9)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'latest.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        score_id = _add_score(session, date(2026, 7, 24))
        _add_analysis(
            session, 7, score_id, started_at=kst(2026, 7, 27, 8, 10),
            finished_at=kst(2026, 7, 27, 8, 30),
        )
        _add_analysis(
            session, 8, score_id, started_at=kst(2026, 7, 27, 8, 20),
            finished_at=kst(2026, 7, 27, 8, 25),
        )
        session.commit()

    summary = AnalysisSummaryRunStore(engine, "mock", now=lambda: now).latest_analysis()

    assert summary is not None
    assert summary.run_id == 7


def test_analysis_summary_materialization은_중복을막고_본문과_outbox를_한트랜잭션에_저장한다(
        tmp_path):
    """idempotency 충돌이 delivery를 중복 생성하거나 payload를 넓히면 실패한다."""
    now = kst(2026, 7, 27, 9)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'outbox.db'}")
    Base.metadata.create_all(engine)
    store = NotificationStore(engine, now=lambda: now)
    summary = MorningAnalysisSummary(
        run_id=7, run_environment="mock", regime="neutral", market_summary="시장 요약",
        max_picks_advice=0, score_reference_date=date(2026, 7, 24),
        started_at=utc(kst(2026, 7, 27, 8, 20)),
        finished_at=utc(kst(2026, 7, 27, 8, 21)), verdicts=(),
    )
    bodies = ("첫 번째 요약", "두 번째 요약")

    first = store.materialize_analysis_summary(summary, bodies, occurred_at=now)
    second = store.materialize_analysis_summary(summary, bodies, occurred_at=now)

    assert first.created is True
    assert second.created is False
    assert first.priority == NotificationPriority.NORMAL
    assert store.count_outbox() == 1
    assert store.outbox_payload(first.outbox_id)["analysis_run_id"] == 7
    assert store.outbox_payload(first.outbox_id) == {
        "version": 1,
        "analysis_run_id": 7,
        "run_environment": "mock",
        "score_reference_date": "2026-07-24",
    }
    assert len(store.delivery_bodies(first.outbox_id)) == len(bodies)
    assert store.generated_analysis_run_ids("mock") == (7,)
    with Session(engine) as session:
        row = session.get(NotificationOutboxRow, first.outbox_id)
        assert row is not None
        assert row.kind == "analysis_summary"
        assert row.priority == NotificationPriority.NORMAL
        assert row.sensitive is True
        assert row.retention_kind == "digest"
        assert row.purge_at - row.created_at == timedelta(hours=24)
