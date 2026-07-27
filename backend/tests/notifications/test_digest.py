from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine

from app.core.operations_control import AccountSnapshotDeferred
from app.domain.notifications.digest import (Digest, DigestAccount, DigestBuilder,
                                             DigestPlanner, DigestSection)
from app.domain.errors import BrokerError
from app.domain.notifications.models import NotificationPriority
from app.store.models import (AnalysisRunRow, Base, CollectionRunRow, ScoreRunRow,
                              TradeRunRow)
from app.store.notification_store import (DigestRunStore, MaterializedNotification,
                                          NotificationStore)


KST = __import__("zoneinfo").ZoneInfo("Asia/Seoul")


def kst(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=KST)


def utc(value):
    return value.astimezone(timezone.utc)


class Calendar:
    KST = KST

    @staticmethod
    def is_trading_day(day):
        return day.weekday() < 5


class DigestAudit:
    def __init__(self, generated=()):
        self.generated = set(generated)
        self.skipped = []

    def generated_digest_days(self, run_environment):
        assert run_environment == "mock"
        return tuple(sorted(self.generated))

    def record_digest_skipped_stale(self, trading_day, run_environment, now):
        self.skipped.append((trading_day, run_environment, now))


@pytest.fixture
def planner():
    return DigestPlanner(Calendar(), DigestAudit(), "mock")


def test_digest는_1610부터_최근7거래일을_오래된순으로_캐치업(planner):
    for day in (date(2026, 7, 16), date(2026, 7, 17),
                date(2026, 7, 20), date(2026, 7, 21)):
        planner.mark_generated(day)
    planner.mark_generated(date(2026, 7, 22))

    assert planner.due_dates(kst(2026, 7, 24, 16, 9)) == ()
    assert planner.due_dates(kst(2026, 7, 24, 16, 10)) == (
        date(2026, 7, 23), date(2026, 7, 24))


def test_digest는_비거래일에_보내지_않는다(planner):
    assert planner.due_dates(kst(2026, 7, 25, 16, 10)) == ()


def test_7거래일보다_오래된누락은_audit으로_종결한다():
    audit = DigestAudit((date(2026, 7, 10),))
    planner = DigestPlanner(Calendar(), audit, "mock")

    due = planner.due_dates(kst(2026, 7, 24, 16, 10))

    assert due == (
        date(2026, 7, 16), date(2026, 7, 17), date(2026, 7, 20),
        date(2026, 7, 21), date(2026, 7, 22), date(2026, 7, 23),
        date(2026, 7, 24))
    assert [item[0] for item in audit.skipped] == (
        [date(2026, 7, 13), date(2026, 7, 14), date(2026, 7, 15)])


class DeferredControl:
    async def account_summary(self, priority="interactive"):
        assert priority == "digest"
        raise AccountSnapshotDeferred("no fresh cache")


class Runs:
    def pipeline_summary(self, trading_day):
        return DigestSection({"collection_status": "done", "analysis_status": "unavailable"},
                             kst(2026, 7, 24, 16, 10))

    def trading_summary(self, trading_day):
        return DigestSection({"order_count": 1, "realized_pnl": -1000},
                             kst(2026, 7, 24, 16, 10))


@pytest.mark.anyio
async def test_digest는_snapshot_deferred를_금액0이아닌_unavailable로_표현한다():
    builder = DigestBuilder(
        DeferredControl(), Runs(), "mock", now=lambda: kst(2026, 7, 24, 16, 10))

    digest = await builder.build(date(2026, 7, 24))

    assert digest.idempotency_key == "digest:mock:2026-07-24"
    assert digest.account.available_deposit is None
    assert digest.account.total_eval is None
    assert digest.account.failed_fields == ("account_snapshot",)
    assert "조회 불가" in digest.body
    assert "0원" not in digest.body


@pytest.mark.anyio
async def test_digest는_broker실패도_금액0이아닌_unavailable로_표현한다():
    class BrokerFailedControl:
        async def account_summary(self, priority="interactive"):
            assert priority == "digest"
            raise BrokerError("unavailable")

    digest = await DigestBuilder(
        BrokerFailedControl(), Runs(), "mock", now=lambda: kst(2026, 7, 24, 16, 10)
    ).build(date(2026, 7, 24))

    assert digest.account.available_deposit is None
    assert digest.account.failed_fields == ("account_snapshot",)


def test_digest_materialization은_env와날짜로_중복을막고_민감본문을_같은TX에_저장한다(
        tmp_path):
    now = kst(2026, 7, 24, 16, 10)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'digest.db'}")
    Base.metadata.create_all(engine)
    store = NotificationStore(engine, now=lambda: now)

    first = store.materialize_digest(
        "digest:mock:2026-07-24", {"total_eval": 1_200_000}, "총평가 1,200,000원",
        occurred_at=now)
    second = store.materialize_digest(
        "digest:mock:2026-07-24", {"total_eval": 1_200_000}, "총평가 1,200,000원",
        occurred_at=now)

    assert first.created is True
    assert second.created is False
    assert isinstance(first, MaterializedNotification)
    assert store.count_outbox() == 1
    assert first.priority == NotificationPriority.DIGEST
    assert store.load_payload(first.outbox_id) == {"total_eval": 1_200_000}
    assert store.load_delivery_bodies(first.outbox_id) == ["총평가 1,200,000원"]


def test_stale_audit은_실행환경별로_독립_idempotency를_가진다(tmp_path):
    now = kst(2026, 7, 24, 16, 10)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'audit.db'}")
    Base.metadata.create_all(engine)
    store = NotificationStore(engine, now=lambda: now)

    store.record_digest_skipped_stale(date(2026, 7, 15), "mock", now)
    store.record_digest_skipped_stale(date(2026, 7, 15), "real", now)

    assert store.operational_event_count() == 2


def test_run_read_model은_실제_run테이블을_허용목록_요약으로_변환한다(tmp_path):
    now = kst(2026, 7, 24, 16, 10)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'runs.db'}")
    Base.metadata.create_all(engine)

    pipeline = DigestRunStore(engine, "mock", now=lambda: now).pipeline_summary(
        date(2026, 7, 24))
    trading = DigestRunStore(engine, "mock", now=lambda: now).trading_summary(
        date(2026, 7, 24))

    assert pipeline.facts["collection_status"] == "unavailable"
    assert pipeline.failed_fields == ("collection", "scoring", "analysis")
    assert trading.facts["current_position_count"] == 0
    assert trading.failed_fields == ("trade_runs",)


def test_digest_section은_민감키와_과도한본문값을_거부한다():
    with pytest.raises(ValueError, match="sensitive"):
        DigestSection({"broker_token": "secret"}, kst(2026, 7, 24, 16, 10))
    with pytest.raises(ValueError, match="too long"):
        DigestSection({"collection_status": "x" * 97}, kst(2026, 7, 24, 16, 10))


def test_digest본문은_각_read_model의_누락필드를_명시한다():
    digest = Digest(
        date(2026, 7, 24), "mock",
        DigestSection({"collection_status": "unavailable"}, None, ("collection",)),
        DigestSection({"order_count": 0}, None, ("trade_runs",)),
        DigestAccount(
            None, None, None, None, "unknown", "unavailable",
            ("account_snapshot",), None, None))

    assert "누락 필드: collection" in digest.body
    assert "누락 필드: trade_runs" in digest.body


def test_run_read_model은_거래일의_전거래일수집과_score기준일을_따른다(tmp_path):
    now = kst(2026, 7, 24, 16, 10)
    reference_day = date(2026, 7, 23)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'timeline.db'}")
    Base.metadata.create_all(engine)
    with __import__("sqlalchemy").orm.Session(engine) as session:
        session.add(CollectionRunRow(
            started_at=utc(kst(2026, 7, 23, 19)),
            finished_at=utc(kst(2026, 7, 23, 19, 5)),
            status="done", total_symbols=10, succeeded=10, failed=0, error_summary=None))
        score = ScoreRunRow(
            started_at=utc(kst(2026, 7, 24, 0, 20)),
            finished_at=utc(kst(2026, 7, 24, 0, 25)),
            status="succeeded", reference_date=reference_day, universe_count=10,
            stale_excluded=0, failure_reason=None, config="{}")
        session.add(score)
        session.flush()
        session.add(AnalysisRunRow(
            started_at=utc(kst(2026, 7, 24, 8, 20)),
            finished_at=utc(kst(2026, 7, 24, 8, 21)),
            status="succeeded", score_run_id=score.id, model="test", prompt_hash="123",
            config="{}", regime="risk_on", market_summary=None, warnings=None,
            failure_reason=None, max_picks_advice=1, economist_fallback=False))
        session.add(TradeRunRow(
            started_at=utc(kst(2026, 7, 24, 9)),
            finished_at=utc(kst(2026, 7, 24, 15, 30)),
            status="succeeded", config="{}", run_environment="mock",
            stopped_by_kill_switch=False, kill_switch_mode=None, warnings=None,
            failure_reason=None))
        session.commit()

    summary = DigestRunStore(engine, "mock", now=lambda: now).pipeline_summary(
        date(2026, 7, 24))

    assert summary.facts["collection_status"] == "done"
    assert summary.facts["collection_reference_day"] == "2026-07-23"
    assert summary.facts["scoring_status"] == "succeeded"
    assert summary.facts["scoring_reference_day"] == "2026-07-23"
