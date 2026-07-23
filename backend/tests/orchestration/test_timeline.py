"""timeline.py 순수 평가기 전수(P6 스펙 §4·§5, 계획 Task 3).

창 경계는 열림(19:00·08:20·09:00 정각 ± 오프바이원)과 닫힘(23:55·08:50·
09:20·15:20)을 대칭으로 고정한다(트레이더 계획 리뷰 — 09:00은 완전
자동매매를 여는 가장 민감한 트리거). 완료 판정·킬스위치 3분기는 store
소유(Task 4)라 여기서는 불리언 입력 계약 기준으로 검증한다."""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.domain.orchestration.config import ScheduleConfig
from app.domain.orchestration.timeline import (Action, Decision, Job,
                                               JobFacts, Reason,
                                               TimelineFacts, evaluate)

KST = timezone(timedelta(hours=9))
CFG = ScheduleConfig()

WED = date(2026, 7, 22)   # 수 — 거래일
THU = date(2026, 7, 23)
FRI = date(2026, 7, 24)
SAT = date(2026, 7, 25)
MON = date(2026, 7, 27)


class Cal:
    KST = KST

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5


CAL = Cal()


def _dt(d: date, h: int, m: int, s: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, s, tzinfo=KST)


def _facts(*, collect=None, score=None, analyze=None, trade=None,
           reference: date = WED, engine=True, paused=False) -> TimelineFacts:
    return TimelineFacts(collect=collect or JobFacts(),
                         score=score or JobFacts(),
                         analyze=analyze or JobFacts(),
                         trade=trade or JobFacts(),
                         score_reference_date=reference,
                         engine_assembled=engine, paused=paused)


def _decision(decisions: tuple[Decision, ...], job: Job) -> Decision:
    (match,) = [d for d in decisions if d.job is job]
    return match


def _eval(now: datetime, facts: TimelineFacts, job: Job) -> Decision:
    return _decision(evaluate(now, facts, CFG, CAL), job)


# ── 공통 계약 ───────────────────────────────────────────────────────────

def test_잡당_정확히_하나의_판정():
    decisions = evaluate(_dt(WED, 12, 0), _facts(), CFG, CAL)
    assert [d.job for d in decisions] == [Job.COLLECT, Job.SCORE,
                                          Job.ANALYZE, Job.TRADE]


def test_paused면_전_잡_휴면():
    now = _dt(WED, 19, 30)
    for d in evaluate(now, _facts(paused=True), CFG, CAL):
        assert (d.action, d.reason) == (Action.WAIT, Reason.PAUSED)


def test_비거래일은_시각_기반_잡_전부_휴면():
    now = _dt(SAT, 19, 30)
    decisions = evaluate(now, _facts(reference=FRI), CFG, CAL)
    for job in (Job.COLLECT, Job.ANALYZE, Job.TRADE):
        assert _decision(decisions, job).reason is Reason.NOT_TRADING_DAY


def test_완료와_실행중은_멱등_대기():
    now = _dt(WED, 19, 30)
    done = _eval(now, _facts(collect=JobFacts(completed=True)), Job.COLLECT)
    assert (done.action, done.reason) == (Action.WAIT, Reason.COMPLETED)
    running = _eval(now, _facts(collect=JobFacts(running=True)), Job.COLLECT)
    assert running.reason is Reason.ALREADY_RUNNING


# ── 수집: 창 경계·백오프 ────────────────────────────────────────────────

def test_수집_열림_경계():
    assert _eval(_dt(WED, 18, 59, 59), _facts(),
                 Job.COLLECT).reason is Reason.WINDOW_NOT_OPEN
    at_open = _eval(_dt(WED, 19, 0, 0), _facts(), Job.COLLECT)
    assert (at_open.action, at_open.reason) == (Action.TRIGGER,
                                                Reason.FIRST_ATTEMPT)


def test_수집_닫힘_경계와_포기_구분():
    inside = _eval(_dt(WED, 23, 54, 59), _facts(), Job.COLLECT)
    assert inside.action is Action.TRIGGER
    # 시도 이력 없이 닫힘 → SKIP, 실패 이력 있으면 GAVE_UP
    no_try = _eval(_dt(WED, 23, 55, 0), _facts(), Job.COLLECT)
    assert (no_try.action, no_try.reason) == (Action.SKIP,
                                              Reason.WINDOW_CLOSED)
    failed = _facts(collect=JobFacts(last_failure_at=_dt(WED, 20, 0)))
    gave_up = _eval(_dt(WED, 23, 55, 0), failed, Job.COLLECT)
    assert (gave_up.action, gave_up.reason) == (Action.GAVE_UP,
                                                Reason.WINDOW_CLOSED)


def test_수집_백오프_600초():
    failed = _facts(collect=JobFacts(last_failure_at=_dt(WED, 19, 5)))
    waiting = _eval(_dt(WED, 19, 14, 59), failed, Job.COLLECT)
    assert (waiting.action, waiting.reason) == (Action.WAIT,
                                                Reason.RETRY_BACKOFF)
    retry = _eval(_dt(WED, 19, 15, 0), failed, Job.COLLECT)
    assert (retry.action, retry.reason) == (Action.RETRY,
                                            Reason.AFTER_FAILURE)


# ── 스코어링: 선행·자정 경계·주말 통과 ──────────────────────────────────

def test_스코어링_선행_미충족은_대기():
    d = _eval(_dt(THU, 0, 30), _facts(), Job.SCORE)
    assert (d.action, d.reason) == (Action.WAIT, Reason.PREREQUISITE_MISSING)


def test_스코어링_창은_자정부터_R일_저녁은_미개장():
    """§4-b 정정(2026-07-23 7b 실사고): R일 저녁 실행은 scoring_reference_
    date 의미론상 reference=R-1로 기록돼 무한 재트리거+아침 신선도 거부 —
    자정을 넘어야 창이 열린다."""
    facts = _facts(collect=JobFacts(completed=True), reference=WED)
    evening = _eval(_dt(WED, 20, 30), facts, Job.SCORE)
    assert (evening.action, evening.reason) == (Action.WAIT,
                                                Reason.WINDOW_NOT_OPEN)
    # 자정 직후부터 트리거(마감 = 다음 거래일 08:50)
    assert _eval(_dt(THU, 0, 0, 30), facts, Job.SCORE).action is Action.TRIGGER
    assert _eval(_dt(THU, 8, 49, 59), facts,
                 Job.SCORE).action is Action.TRIGGER
    closed = _eval(_dt(THU, 8, 50, 0), facts, Job.SCORE)
    assert (closed.action, closed.reason) == (Action.SKIP,
                                              Reason.WINDOW_CLOSED)


def test_스코어링_백오프_600초():
    """잡별 백오프 전수의 4번째(개발자 T3 — score_retry_backoff_s 배선
    자체를 고정하는 유일한 지점)."""
    facts = _facts(collect=JobFacts(completed=True),
                   score=JobFacts(last_failure_at=_dt(THU, 0, 20)),
                   reference=WED)
    waiting = _eval(_dt(THU, 0, 29, 59), facts, Job.SCORE)
    assert (waiting.action, waiting.reason) == (Action.WAIT,
                                                Reason.RETRY_BACKOFF)
    retry = _eval(_dt(THU, 0, 30, 0), facts, Job.SCORE)
    assert (retry.action, retry.reason) == (Action.RETRY,
                                            Reason.AFTER_FAILURE)


def test_결함_calendar는_30일_상한에서_실패한다():
    class DeadCal:
        KST = KST

        def is_trading_day(self, d):
            return False

    facts = _facts(collect=JobFacts(completed=True), reference=WED)
    with pytest.raises(ValueError, match="calendar defective"):
        # 자정 이후(창 개장 — §4-b 정정)라야 마감 계산 경로에 도달한다
        evaluate(_dt(THU, 0, 30), facts, CFG, DeadCal())


def test_스코어링_금요일_몫은_주말에도_재시도_가능_월요일_아침_마감():
    """스펙 §4-b: 스코어링은 경량이라 비거래일 휴면 없이 흐른다 —
    마감만 '다음 거래일(월) 08:50'."""
    facts = _facts(collect=JobFacts(completed=True),
                   score=JobFacts(last_failure_at=_dt(FRI, 21, 0)),
                   reference=FRI)
    assert _eval(_dt(SAT, 10, 0), facts, Job.SCORE).action is Action.RETRY
    assert _eval(_dt(MON, 8, 49), facts, Job.SCORE).action is Action.RETRY
    assert _eval(_dt(MON, 8, 50), facts, Job.SCORE).action is Action.GAVE_UP


# ── 분석: 창 경계·백오프 300초 ──────────────────────────────────────────

def test_분석_열림_경계():
    assert _eval(_dt(THU, 8, 19, 59), _facts(),
                 Job.ANALYZE).reason is Reason.WINDOW_NOT_OPEN
    assert _eval(_dt(THU, 8, 20, 0), _facts(),
                 Job.ANALYZE).action is Action.TRIGGER


def test_분석_백오프와_닫힘_경계():
    failed = _facts(analyze=JobFacts(last_failure_at=_dt(THU, 8, 20)))
    assert _eval(_dt(THU, 8, 24, 59), failed,
                 Job.ANALYZE).reason is Reason.RETRY_BACKOFF
    assert _eval(_dt(THU, 8, 25, 0), failed,
                 Job.ANALYZE).action is Action.RETRY
    assert _eval(_dt(THU, 9, 19, 59), failed,
                 Job.ANALYZE).action is Action.RETRY
    assert _eval(_dt(THU, 9, 20, 0), failed,
                 Job.ANALYZE).action is Action.GAVE_UP


# ── 트레이딩: 열림 09:00·포기 없음·엔진 미조립 ─────────────────────────

def test_트레이딩_열림_경계_0900():
    """완전 자동매매를 여는 가장 민감한 트리거(트레이더 계획 리뷰)."""
    assert _eval(_dt(THU, 8, 59, 59), _facts(),
                 Job.TRADE).reason is Reason.WINDOW_NOT_OPEN
    at_open = _eval(_dt(THU, 9, 0, 0), _facts(), Job.TRADE)
    assert (at_open.action, at_open.reason) == (Action.TRIGGER,
                                                Reason.FIRST_ATTEMPT)


def test_트레이딩_60초_백오프_무한_재시도():
    """max_attempts 없음 — 실패 횟수와 무관하게 창 내내 60초 간격 재시도
    (스펙 §5: 포기는 보유 포지션 감시 마비)."""
    failed = _facts(trade=JobFacts(last_failure_at=_dt(THU, 12, 0)))
    assert _eval(_dt(THU, 12, 0, 59), failed,
                 Job.TRADE).reason is Reason.RETRY_BACKOFF
    assert _eval(_dt(THU, 12, 1, 0), failed, Job.TRADE).action is Action.RETRY
    assert _eval(_dt(THU, 15, 19, 59), failed,
                 Job.TRADE).action is Action.RETRY
    assert _eval(_dt(THU, 15, 20, 0), failed,
                 Job.TRADE).action is Action.GAVE_UP


def test_트레이딩_엔진_미조립은_스킵():
    d = _eval(_dt(THU, 10, 0), _facts(engine=False), Job.TRADE)
    assert (d.action, d.reason) == (Action.SKIP, Reason.ENGINE_NOT_ASSEMBLED)


def test_트레이딩_완료는_킬스위치_포함_불리언_계약():
    """succeeded/kill-switch stopped(=완료) vs 셧다운 stopped/failed(=미완료)
    3분기는 store 판정(Task 4) — 여기서는 불리언 반영만 고정."""
    done = _eval(_dt(THU, 10, 0), _facts(trade=JobFacts(completed=True)),
                 Job.TRADE)
    assert (done.action, done.reason) == (Action.WAIT, Reason.COMPLETED)
    crashed = _facts(trade=JobFacts(completed=False,
                                    last_failure_at=_dt(THU, 9, 30)))
    assert _eval(_dt(THU, 10, 0), crashed, Job.TRADE).action is Action.RETRY


def test_캐치업_낮_재부팅은_별도_메커니즘_없이_트리거():
    """결정 #40 — 13:00 재기동 첫 evaluate: 트레이딩 몫 미완이면 즉시
    트리거(감시 재개), 수집은 창 밖 대기."""
    decisions = evaluate(_dt(THU, 13, 0), _facts(), CFG, CAL)
    assert _decision(decisions, Job.TRADE).action is Action.TRIGGER
    assert _decision(decisions,
                     Job.COLLECT).reason is Reason.WINDOW_NOT_OPEN


def test_decision_reason은_자유_텍스트를_거부한다():
    """P6-T3 보안 Important — reason은 무인증 노출 표면: 예외 원문 등
    자유 문자열은 생성 시점에 fail-loud."""
    with pytest.raises(TypeError, match="no free text"):
        Decision(Job.TRADE, Action.SKIP, "ConnectionError: dsn=...")
    with pytest.raises(TypeError, match="Action"):
        Decision(Job.TRADE, "skipped", Reason.WINDOW_CLOSED)


# ── ScheduleConfig 검증 ─────────────────────────────────────────────────

def test_config_창_순서_검증():
    from datetime import time
    with pytest.raises(ValueError, match="trade window"):
        ScheduleConfig(trade_start_at=time(15, 30), trade_until=time(9, 0))


def test_config_양수_검증():
    with pytest.raises(ValueError, match="tick_interval_s"):
        ScheduleConfig(tick_interval_s=0)
    with pytest.raises(ValueError, match="trade_retry_backoff_s"):
        ScheduleConfig(trade_retry_backoff_s=-1)
