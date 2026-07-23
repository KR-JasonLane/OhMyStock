"""SchedulerStore + 소유 스토어 판정 헬퍼(P6 Task 4, 스펙 §6).

핵심 회귀: ① UTC/KST 날짜 경계 — 08:20 KST(=UTC 전날 23:20) 시작 런이
올바른 거래일로 판정(계획 Task 4 개발자 Critical), ② 트레이딩 완료의
stopped_by_kill_switch 3분기(§4-d), ③ 이벤트 적재의 enum 강제(고정 리터럴
계약 이중 방어), ④ 리플레이 환경 교차 오염 차단."""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine

from app.domain.orchestration.config import ScheduleConfig
from app.domain.orchestration.timeline import (Action, Job, Reason,
                                               score_reference_for)
from app.store.analysis_store import AnalysisStore
from app.store.collection_store import CollectionStore
from app.store.models import Base
from app.store.scheduler_store import SchedulerStore
from app.store.scoring_store import ScoringStore
from app.store.trading_store import TradingStore

KST = timezone(timedelta(hours=9))
WED = date(2026, 7, 22)
THU = date(2026, 7, 23)
# 목 08:20 KST = 수 23:20 UTC — SQL DATE() 비교가 오분류하는 정확한 경계
THU_0820_KST_AS_UTC = datetime(2026, 7, 22, 23, 20, tzinfo=timezone.utc)


class _Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def clock() -> _Clock:
    return _Clock(datetime(2026, 7, 22, 11, 0, tzinfo=timezone.utc))


@pytest.fixture
def stores(tmp_path, clock):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'sched.db'}")
    Base.metadata.create_all(engine)
    collection = CollectionStore(engine, now=clock)
    scoring = ScoringStore(engine, now=clock)
    analysis = AnalysisStore(engine, now=clock)
    trading = TradingStore(engine, now=clock)
    scheduler = SchedulerStore(engine, collection, scoring, analysis, trading,
                               run_environment="mock", now=clock)
    return scheduler, collection, scoring, analysis, trading, clock


# ── UTC/KST 날짜 경계 (개발자 Critical) ────────────────────────────────

def test_아침_0820_시작_분석런은_당일로_판정된다(stores):
    """UTC 저장(수 23:20Z) = 목 08:20 KST — 목요일 몫으로 판정돼야 한다.
    naive DATE() 비교였다면 수요일로 오분류돼 창 내 반복 재트리거."""
    scheduler, _, scoring, analysis, _, clock = stores
    score_run = scoring.create_run(WED, "{}")
    scoring.finish_run(score_run, "succeeded")
    clock.t = THU_0820_KST_AS_UTC
    run_id = analysis.create_run(score_run, "m", "hash", "{}")
    analysis.finish_run(run_id, "succeeded")
    assert analysis.has_completed_run(THU) is True
    assert analysis.has_completed_run(WED) is False


def test_수집런_KST_경계와_실패_시각(stores):
    _, collection, *_rest, clock = stores
    clock.t = THU_0820_KST_AS_UTC          # 목 08:20 KST
    run_id = collection.create_run()
    clock.t = THU_0820_KST_AS_UTC + timedelta(minutes=5)
    collection.finish_run(run_id, "failed", 0, 0, 0)
    assert collection.has_completed_run(THU) is False
    done = collection.create_run()
    collection.finish_run(done, "done", 10, 10, 0)   # 실서비스 완료 리터럴
    assert collection.has_completed_run(THU) is True
    stamp = collection.last_failed_finished_at(THU)
    if stamp.tzinfo is None:      # sqlite는 naive 저장(프로덕션은 aware UTC)
        stamp = stamp.replace(tzinfo=timezone.utc)
    assert stamp == clock.t
    assert collection.last_failed_finished_at(WED) is None


# ── 스코어링: reference_date 기반 ───────────────────────────────────────

def test_스코어링은_reference_date로_판정(stores):
    _, _, scoring, *_ = stores
    ok = scoring.create_run(WED, "{}")
    scoring.finish_run(ok, "succeeded")
    bad = scoring.create_run(THU, "{}")
    scoring.finish_run(bad, "failed")
    assert scoring.has_completed_run(WED) is True
    assert scoring.has_completed_run(THU) is False
    assert scoring.last_failed_finished_at(THU) is not None
    assert scoring.last_failed_finished_at(WED) is None


# ── 트레이딩: §4-d 3분기 + 환경 필터 ───────────────────────────────────

def test_트레이딩_완료는_킬스위치_stopped만(stores):
    """succeeded/kill-stopped=완료, 셧다운 stopped/failed=미완료+백오프
    대상(개발자 계획 Critical — 컬럼 미검사 시 크래시 재기동이 완료 오판)."""
    *_, trading, clock = stores
    run1 = trading.create_run("{}", "mock")
    trading.finish_run(run1, "stopped", stopped_by_kill_switch=False,
                       failure_reason="cancelled (shutdown)")
    assert trading.has_completed_run(WED, "mock") is False
    assert trading.last_failed_finished_at(WED, "mock") is not None
    run2 = trading.create_run("{}", "mock")
    trading.finish_run(run2, "stopped", stopped_by_kill_switch=True,
                       kill_switch_mode="liquidate_all")
    assert trading.has_completed_run(WED, "mock") is True


def test_트레이딩런_KST_경계(stores):
    """전 스토어 UTC 경계 요구(계획 Task 4)의 트레이딩 분 — 목 08:20 KST
    (=수 23:20Z) 시작 run은 목요일 몫(개발자 T4 Important)."""
    *_, trading, clock = stores
    clock.t = THU_0820_KST_AS_UTC
    run_id = trading.create_run("{}", "mock")
    trading.finish_run(run_id, "succeeded")
    assert trading.has_completed_run(THU, "mock") is True
    assert trading.has_completed_run(WED, "mock") is False


def test_트레이딩_리플레이_런은_모의_몫에_불산입(stores):
    *_, trading, _clock = stores
    replay = trading.create_run("{}", "replay")
    trading.finish_run(replay, "succeeded")
    assert trading.has_completed_run(WED, "mock") is False
    assert trading.has_completed_run(WED, "replay") is True


# ── build_job_facts 합성 ────────────────────────────────────────────────

def test_build_job_facts는_R과_today를_분리해_합성(stores):
    """collect/score 몫=R(수), analyze/trade 몫=today(목) — §4-b 계약."""
    scheduler, collection, scoring, analysis, trading, clock = stores
    run = collection.create_run()
    # 수집 완료 리터럴은 "done"(P2 유래 — 타 서비스 "succeeded"와 다름).
    # "succeeded"로 가정한 원 테스트가 실사고(무한 재트리거)를 못 잡았다 —
    # 실서비스 리터럴로 고정(2026-07-23 7b 발견 회귀).
    collection.finish_run(run, "done", 10, 10, 0)          # 수요일(UTC 11시)
    score_run = scoring.create_run(WED, "{}")
    scoring.finish_run(score_run, "succeeded")
    clock.t = THU_0820_KST_AS_UTC
    a_run = analysis.create_run(score_run, "m", "h", "{}")
    analysis.finish_run(a_run, "succeeded")
    facts = scheduler.build_job_facts(reference=WED, today=THU)
    assert facts[Job.COLLECT].completed is True
    assert facts[Job.SCORE].completed is True
    assert facts[Job.ANALYZE].completed is True
    assert facts[Job.TRADE].completed is False
    assert all(not f.running for f in facts.values())   # running은 호출자 소관


# ── 이벤트 적재 (§6 고정 리터럴 계약) ──────────────────────────────────

def test_record_event_왕복과_최근순(stores):
    scheduler, *_ = stores
    scheduler.record_event(Job.COLLECT, Action.TRIGGER, Reason.FIRST_ATTEMPT,
                           run_id=7)
    scheduler.record_event(Job.TRADE, Action.START_REJECTED, Reason.CONFLICT)
    events = scheduler.recent_events(limit=5)
    assert [e["job"] for e in events] == ["trade", "collect"]
    assert events[0]["reason"] == "conflict"
    assert events[1]["run_id"] == 7


def test_record_event는_enum만_수용(stores):
    scheduler, *_ = stores
    with pytest.raises(TypeError, match="enum members only"):
        scheduler.record_event("collect", Action.TRIGGER,
                               Reason.FIRST_ATTEMPT)
    with pytest.raises(TypeError, match="enum members only"):
        scheduler.record_event(Job.COLLECT, Action.TRIGGER,
                               "ConnectionError: dsn=...")
    with pytest.raises(TypeError, match="enum members only"):
        scheduler.record_event(Job.COLLECT, "triggered",
                               Reason.FIRST_ATTEMPT)
    with pytest.raises(ValueError, match="WAIT is a state"):
        scheduler.record_event(Job.COLLECT, Action.WAIT, Reason.COMPLETED)


# ── score_reference_for (자정 경계 R 산정) ─────────────────────────────

class _Cal:
    KST = KST

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5


def test_score_reference_저녁이면_오늘_아침이면_직전_거래일():
    cfg = ScheduleConfig()
    cal = _Cal()
    evening = datetime(2026, 7, 22, 19, 0, tzinfo=KST)     # 수 19:00 정각
    assert score_reference_for(evening, cfg, cal) == WED
    before = datetime(2026, 7, 22, 18, 59, 59, tzinfo=KST)
    assert score_reference_for(before, cfg, cal) == date(2026, 7, 21)
    morning = datetime(2026, 7, 23, 8, 30, tzinfo=KST)     # 목 아침
    assert score_reference_for(morning, cfg, cal) == WED
    saturday = datetime(2026, 7, 25, 20, 0, tzinfo=KST)    # 토 저녁
    assert score_reference_for(saturday, cfg, cal) == date(2026, 7, 24)
