from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine

from app.core.operations_control import AccountSnapshotDeferred
from app.domain.notifications.digest import (Digest, DigestAccount, DigestBuilder,
                                             DigestPlanner, DigestSection,
                                             render_retained_digest)
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
    assert "계좌 스냅샷을 조회하지 못했습니다." in digest.body
    account_section = digest.body.split("💰 계좌\n", 1)[1].split("\n\n", 1)[0]
    assert "0원" not in account_section


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

    assert "- 데이터 수집 결과 없음" in digest.body
    assert "- 거래 실행 기록 없음" in digest.body
    assert "누락 필드:" not in digest.body


def _readable_digest(
    *,
    run_environment="mock",
    market_regime="risk_off",
    pick_count=0,
    candidate_count=2519,
    entry_order_count=0,
    exit_order_count=0,
    current_position_count=0,
    realized_pnl=0,
    realized_pnl_confidence="estimated",
    pipeline_failed=(),
    trading_failed=(),
    account_source="cached",
    account_total_eval=0,
    account_failed=(),
    account_confidence="estimated",
    account_trading_day=date(2026, 7, 28),
):
    return Digest(
        date(2026, 7, 28),
        run_environment,
        DigestSection(
            {
                "collection_status": "done",
                "collection_reference_day": "2026-07-27",
                "scoring_status": "succeeded",
                "scoring_reference_day": "2026-07-27",
                "candidate_count": candidate_count,
                "analysis_status": "succeeded",
                "analysis_score_reference_day": "2026-07-27",
                "pick_count": pick_count,
                "market_regime": market_regime,
            },
            kst(2026, 7, 28, 8, 28),
            pipeline_failed,
        ),
        DigestSection(
            {
                "order_count": entry_order_count + exit_order_count,
                "entry_order_count": entry_order_count,
                "exit_order_count": exit_order_count,
                "current_position_count": current_position_count,
                "realized_pnl": realized_pnl,
                "realized_pnl_confidence": realized_pnl_confidence,
                "kill_switch_run_count": 0,
                "scheduler_gave_up_count": 0,
                "scheduler_dead_count": 0,
                "dead_letter_count": 0,
            },
            kst(2026, 7, 28, 16, 10),
            trading_failed,
        ),
        DigestAccount(
            9_979_053 if account_source != "unavailable" else None,
            account_total_eval if account_source != "unavailable" else None,
            0 if account_source != "unavailable" else None,
            0 if account_source != "unavailable" else None,
            account_confidence,
            account_source,
            account_failed,
            kst(2026, 7, 28, 16, 10),
            account_trading_day,
        ),
    )


def test_digest본문은_정상상태를_한국어핵심요약으로_표시한다():
    digest = _readable_digest()

    assert digest.body == (
        "📋 장 마감 다이제스트 · 모의투자\n"
        "2026년 7월 28일\n\n"
        "📊 오늘의 분석\n"
        "데이터 수집      완료 · 기준 7월 27일\n"
        "종목 점수 계산   완료 · 2,519종목\n"
        "AI 분석          완료 · 위험회피\n"
        "최종 진입 후보   없음\n\n"
        "💼 자동매매\n"
        "매수 주문        0건\n"
        "매도 주문        0건\n"
        "현재 관리 포지션 0개\n"
        "실현손익         0원 (추정)\n\n"
        "💰 계좌\n"
        "주문 가능        9,979,053원\n"
        "보유주식 평가    0원\n"
        "평가손익         0원\n"
        "실현손익         0원 (추정)\n\n"
        "🕖 다음 일정\n"
        "오늘 19:00 데이터 수집"
    )


@pytest.mark.parametrize(
    ("stored", "displayed"),
    [
        ("risk_on", "위험선호"),
        ("neutral", "중립"),
        ("risk_off", "위험회피"),
        ("unexpected", "확인 불가"),
        (None, "확인 불가"),
    ],
)
def test_digest본문은_시장국면을_허용목록으로_표시한다(stored, displayed):
    digest = _readable_digest(market_regime=stored)

    expected = (
        f"완료 · {displayed}"
        if displayed != "확인 불가"
        else "확인 불가"
    )
    assert f"AI 분석          {expected}" in digest.body


def test_digest본문은_실전환경과_후보_양수와_exact손익을_표시한다():
    digest = _readable_digest(
        run_environment="real",
        pick_count=3,
        realized_pnl=-12_345,
        realized_pnl_confidence="exact",
    )

    assert digest.body.startswith("📋 장 마감 다이제스트 · 🚨 실전")
    assert "최종 진입 후보   3종목" in digest.body
    assert "실현손익         -12,345원" in digest.body
    assert "-12,345원 (추정)" not in digest.body


def test_digest본문은_누락과_손상값을_추측없이_경고한다():
    digest = _readable_digest(
        pipeline_failed=("collection", "scoring", "unknown_internal"),
        trading_failed=("trade_runs", "unknown_internal"),
        account_source="unavailable",
        account_failed=("account_snapshot", "unknown_internal"),
        candidate_count=-1,
        entry_order_count=-1,
    )

    assert "계좌 스냅샷을 조회하지 못했습니다." in digest.body
    assert digest.body.count("- 일부 상태 확인 불가") == 1
    assert digest.body.count("- 데이터 수집 결과 없음") == 1
    assert digest.body.count("- 종목 점수 계산 결과 없음") == 1
    assert digest.body.count("- 거래 실행 기록 없음") == 1
    assert digest.body.count("- 계좌 스냅샷 조회 실패") == 1
    assert "unknown_internal" not in digest.body
    assert "매수 주문        확인 불가" in digest.body


def test_digest본문은_기술식별자_JSON_UTC시각을_노출하지않는다():
    body = _readable_digest().body

    assert "digest:mock:" not in body
    assert "다이제스트 ID" not in body
    assert '{"' not in body
    assert "+00:00" not in body
    assert "T08:" not in body


def test_digest본문은_계좌부분실패에서_가용금액만_보존한다():
    digest = _readable_digest(
        account_total_eval=None,
        account_failed=("total_eval",),
    )

    assert "주문 가능        9,979,053원" in digest.body
    assert "보유주식 평가    확인 불가" in digest.body
    assert "- 일부 계좌 정보 확인 불가" in digest.body
    assert "total_eval" not in digest.body


def test_digest본문은_catchup계좌를_현재스냅샷으로_명시한다():
    digest = _readable_digest(account_trading_day=date(2026, 7, 29))

    assert "현재 관리 포지션 0개" in digest.body
    assert "현재 계좌 · 기준 7월 29일" in digest.body
    assert "- 계좌 정보는 다이제스트 거래일 기준이 아님" in digest.body


def test_digest본문은_계좌기준일누락을_경고한다():
    digest = _readable_digest(account_trading_day=None)

    assert "- 계좌 기준일 확인 불가" in digest.body


def test_digest본문은_알수없는계좌출처를_fail_closed한다():
    digest = _readable_digest(account_source="unknown")

    assert "계좌 정보의 출처를 확인하지 못했습니다." in digest.body
    assert "9,979,053원" not in digest.body
    assert "- 일부 계좌 정보 확인 불가" in digest.body


def test_digest본문은_알수없는계좌손익신뢰도를_fail_closed한다():
    digest = _readable_digest(account_confidence="unknown")

    account_section = digest.body.split("💰 계좌\n", 1)[1].split("\n\n", 1)[0]
    assert "실현손익         확인 불가" in account_section
    assert "- 일부 계좌 정보 확인 불가" in digest.body


def test_digest본문은_failed_fields가없어도_누락계좌금액을_경고한다():
    digest = _readable_digest(account_total_eval=None)

    assert "보유주식 평가    확인 불가" in digest.body
    assert "- 일부 계좌 정보 확인 불가" in digest.body


def test_retained_v1은_새_digest본문과_동일한_presenter를_사용한다():
    digest = _readable_digest()

    assert render_retained_digest(deepcopy(digest.payload)) == digest.body


def _retained_digest() -> Digest:
    return Digest(
        date(2026, 7, 24), "mock",
        DigestSection({"collection_status": "done"}, kst(2026, 7, 24, 16, 10)),
        DigestSection({"order_count": 1}, kst(2026, 7, 24, 16, 10)),
        DigestAccount(
            1_000, 2_000, 30, 40, "estimated", "cached", (),
            kst(2026, 7, 24, 16, 10), date(2026, 7, 24)),
    )


def test_retained_digest_payload은_원래_digest본문으로_재표시한다():
    """보존 payload를 현재 계좌 값으로 재계산하면 실패한다."""
    digest = _retained_digest()

    assert render_retained_digest(digest.payload) == digest.body


def test_digest는_지원하지않는_환경을_거부한다():
    """지원하지 않는 환경이 보존 payload에 섞이면 fail-closed해야 한다."""
    digest = _retained_digest()

    with pytest.raises(ValueError, match="run_environment"):
        Digest(
            date(2026, 7, 24), "replay", digest.pipeline, digest.trading, digest.account)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.__setitem__("version", 2),
        lambda payload: payload.__setitem__("run_environment", 1),
        lambda payload: payload.__setitem__("trading_day", "not-a-date"),
        lambda payload: payload.__setitem__("pipeline", []),
        lambda payload: payload.__setitem__("account", []),
        lambda payload: payload["account"].__setitem__("available_deposit", "1000"),
    ],
)
def test_retained_digest_payload은_필수_schema가_손상되면_거부한다(mutate):
    """과거 payload의 누락/형변환을 정상 다이제스트처럼 표시하면 실패한다."""
    payload = deepcopy(_retained_digest().payload)
    mutate(payload)

    with pytest.raises(ValueError):
        render_retained_digest(payload)


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
