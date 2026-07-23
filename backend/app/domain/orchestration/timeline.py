"""데일리 타임라인 순수 평가기(P6 스펙 §3·§4·§5) — I/O·await 금지.

exit_rules와 동일 원칙(판정과 부수효과의 물리적 분리): 이 모듈은 "지금 이
잡을 실행해야 하는가"만 계산하고, DB 질의(TimelineFacts 구성)와 start()
호출·이벤트 기록은 SchedulerService(Task 5)/scheduler_store(Task 4) 소관.
잡별 서브평가기로 분리해 4잡×(창/선행/완료/재시도) 조합이 한 함수에
몰리지 않게 한다(계획 리뷰 개발자).

완료 판정은 전부 DB 기준의 불리언으로 주입된다(facts) — 재기동/재부팅 후
첫 evaluate가 곧 캐치업이다(결정 #40, 별도 캐치업 메커니즘 없음).

시각 의미론: 모든 비교는 KST 벽시계(now를 calendar.KST로 변환). 창 판정은
"열림 경계 포함(>=), 닫힘 경계 미포함(<)" — 09:00:00 정각 틱은 트리거,
15:20:00 정각 틱은 창 밖(스펙 §4 표. 열림/닫힘 경계는 트레이더 계획 리뷰
요구로 테스트가 대칭 전수한다)."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum

from app.domain.orchestration.config import ScheduleConfig


class Job(Enum):
    COLLECT = "collect"
    SCORE = "score"
    ANALYZE = "analyze"
    TRADE = "trade"


class Action(Enum):
    """평가 결과 액션. TRIGGER/RETRY만 실행 지시(start() 호출 대상)이고
    나머지는 상태 서술 — 이벤트 기록(action 문자열은 스펙 §6 어휘)은
    SchedulerService가 상태 전이 시에만 수행해 틱마다 중복 적재를 막는다.

    START_REJECTED는 evaluate()가 산출하지 않는 **서비스측 전용** 값이다
    (start()가 None을 반환한 뒤에만 알 수 있음 — Task 5 소관): 이벤트
    어휘를 스펙 §6과 1:1로 이 enum 하나에 모아 store 경계에서 fail-loud
    검증할 수 있게 한다(P6 Task 4)."""
    TRIGGER = "triggered"      # 첫 시도 실행
    RETRY = "retry"            # 실패 후 재시도 실행
    WAIT = "wait"              # 창 밖/선행 대기/백오프/완료/휴면 — 무동작(이벤트 비대상)
    SKIP = "skipped"           # 오늘 몫 실행 불가 확정(시도 없이 — 예: 엔진 미조립)
    GAVE_UP = "gave_up"        # 창 종료 시점까지 실패 잔존(스펙 §5 — 포기)
    START_REJECTED = "start_rejected"  # 서비스측: start()가 None(충돌/실행 중)


class Reason(Enum):
    """고정 사유 리터럴(스펙 §6) — 자유 텍스트 금지: 이 값은 무인증
    /schedule/status와 scheduler_events로 노출된다. 예외 원문·심볼·금액은
    절대 여기로 유입되지 않는다(ERROR 로그 전용)."""
    NOT_TRADING_DAY = "not_trading_day"
    PAUSED = "paused"
    ALREADY_RUNNING = "already_running"
    COMPLETED = "completed"
    WINDOW_NOT_OPEN = "window_not_open"
    WINDOW_CLOSED = "window_closed"
    PREREQUISITE_MISSING = "prerequisite_missing"
    ENGINE_NOT_ASSEMBLED = "engine_not_assembled"
    RETRY_BACKOFF = "retry_backoff"
    FIRST_ATTEMPT = "first_attempt"
    AFTER_FAILURE = "after_failure"
    # ── 서비스측 전용(evaluate() 미산출 — Task 5가 이벤트에 사용) ──
    CONFLICT = "conflict"                  # start() None — 타 서비스 배타/자체 실행 중
    EXECUTION_ERROR = "execution_error"    # Decision 실행 중 예외(원문은 로그 전용)


@dataclass(frozen=True)
class JobFacts:
    """한 잡의 "현재 몫" DB 상태(구성은 scheduler_store — Task 4).

    completed: 몫 완료 판정 — 트레이딩은 `succeeded OR (stopped AND
    stopped_by_kill_switch)`(스펙 §4-d: 셧다운 취소 'stopped'는 미완료 —
    판정 질의가 store 소유라 여기서는 불리언). running과는 배타적으로
    주입된다(running이면 completed=False).
    last_failure_at: 몫에 대한 당일 마지막 실패 run의 종료 시각(재시도
    백오프 기준 — DB 유래라 재부팅 후에도 정확)."""
    completed: bool = False
    running: bool = False
    last_failure_at: datetime | None = None


@dataclass(frozen=True)
class TimelineFacts:
    """evaluate 입력 — 전부 값(DB 질의·설정의 스냅샷). paused는 스케줄러
    인메모리 상태, engine_assembled는 TRADE_* 조립 여부(Settings 유래)."""
    collect: JobFacts
    score: JobFacts
    analyze: JobFacts
    trade: JobFacts
    score_reference_date: date   # 스코어링 몫의 수집 기준일 R(§4-b 자정 경계)
    engine_assembled: bool = True
    paused: bool = False


@dataclass(frozen=True)
class Decision:
    """평가 판정 1건. reason은 무인증 /schedule/status·scheduler_events로
    직행하는 표면이라 **생성 시점에 enum 멤버임을 fail-loud로 강제**한다
    (P6-T3 보안 Important — 타입 힌트만으로는 Task 5가 실수로
    `str(exc)`를 넣어도 통과한다: 예외 원문의 무인증 노출 차단)."""
    job: Job
    action: Action
    reason: Reason

    def __post_init__(self) -> None:
        if not isinstance(self.job, Job):
            raise TypeError(f"job must be a Job enum member: {self.job!r}")
        if not isinstance(self.action, Action):
            raise TypeError(
                f"action must be an Action enum member: {self.action!r}")
        if not isinstance(self.reason, Reason):
            raise TypeError(
                f"reason must be a Reason enum member (no free text — "
                f"unauthenticated exposure surface): {type(self.reason).__name__}")


def _common_gate(job_facts: JobFacts, paused: bool) -> Reason | None:
    """전 잡 공통 선차단 — 해당되면 WAIT 사유, 아니면 None."""
    if paused:
        return Reason.PAUSED
    if job_facts.running:
        return Reason.ALREADY_RUNNING
    if job_facts.completed:
        return Reason.COMPLETED
    return None


def _attempt(job: Job, facts: JobFacts, now: datetime,
             backoff_s: int) -> Decision:
    """창 안·선행 충족 상태의 실행/백오프 판정(잡 공통 꼬리)."""
    if facts.last_failure_at is None:
        return Decision(job, Action.TRIGGER, Reason.FIRST_ATTEMPT)
    if now < facts.last_failure_at + timedelta(seconds=backoff_s):
        return Decision(job, Action.WAIT, Reason.RETRY_BACKOFF)
    return Decision(job, Action.RETRY, Reason.AFTER_FAILURE)


def _closed(job: Job, facts: JobFacts) -> Decision:
    """창 종료 후 미완료 확정 — 실패 이력이 있으면 GAVE_UP(재시도하다
    끝남), 없으면 SKIP(시도 조건이 끝내 안 갖춰짐 — 예: 선행 미충족)."""
    action = Action.GAVE_UP if facts.last_failure_at else Action.SKIP
    return Decision(job, action, Reason.WINDOW_CLOSED)


def _eval_collect(kst: datetime, facts: TimelineFacts, config: ScheduleConfig,
                  trading_day: bool) -> Decision:
    gate = _common_gate(facts.collect, facts.paused)
    if gate is not None:
        return Decision(Job.COLLECT, Action.WAIT, gate)
    if not trading_day:
        return Decision(Job.COLLECT, Action.WAIT, Reason.NOT_TRADING_DAY)
    t = kst.time()
    if t < config.collect_at:
        return Decision(Job.COLLECT, Action.WAIT, Reason.WINDOW_NOT_OPEN)
    if t >= config.collect_until:
        return _closed(Job.COLLECT, facts.collect)
    return _attempt(Job.COLLECT, facts.collect, kst,
                    config.collect_retry_backoff_s)


def _eval_score(kst: datetime, facts: TimelineFacts, config: ScheduleConfig,
                calendar) -> Decision:
    """스코어링 몫 R(=facts.score_reference_date)의 창:
    **R 다음 날 자정(00:00) ~ R 다음 거래일 score_until**.

    ⚠️ 창 시작이 "수집 완료 직후(저녁)"가 아니라 자정인 이유(스펙 §4-b
    정정, 2026-07-23 7b 실사고): ScoringService의 기준일은
    `scoring_reference_date` = "오늘 **이전** 마지막 평일"(P3 자정 배치
    의미론 — market_calendar 독스트링)이라, R일 저녁에 실행하면 run이
    reference=R-1로 기록된다 → ① 스케줄러 몫 판정(reference==R)과 영구
    불일치 = 성공한 스코어링을 30초마다 무한 재트리거(실측: 2분에 4 run),
    ② 그 결과로 아침 분석 signal_date=R-1이 되어 다음 날 진입 신선도
    가드에서 전부 거부(자동매매 무력화). 자정 이후 실행이면
    scoring_reference_date == R로 일치한다. 주말은 휴면 없이 흐른다
    (금 수집 → 토 00:00 스코어링, 마감은 다음 거래일 월 08:50)."""
    gate = _common_gate(facts.score, facts.paused)
    if gate is not None:
        return Decision(Job.SCORE, Action.WAIT, gate)
    if kst.date() <= facts.score_reference_date:
        # R일 당일(저녁 포함)은 창 미개장 — 자정을 넘어야 기준일 정합
        return Decision(Job.SCORE, Action.WAIT, Reason.WINDOW_NOT_OPEN)
    deadline_day = _next_trading_day(facts.score_reference_date, calendar)
    deadline = datetime.combine(deadline_day, config.score_until,
                                tzinfo=kst.tzinfo)
    if kst >= deadline:
        return _closed(Job.SCORE, facts.score)
    if not facts.collect.completed:
        # 선행 = 수집(R) succeeded. facts 구성이 R 기준이라 collect 몫과
        # score 몫은 같은 R을 가리킨다(Task 4 계약).
        return Decision(Job.SCORE, Action.WAIT, Reason.PREREQUISITE_MISSING)
    return _attempt(Job.SCORE, facts.score, kst,
                    config.score_retry_backoff_s)


def _eval_analyze(kst: datetime, facts: TimelineFacts, config: ScheduleConfig,
                  trading_day: bool) -> Decision:
    """분석(E일 아침 — 결정 #38). 선행조건 없음: 연쇄 신선도 게이트가 자체
    검증하므로(스펙 §4 표) 스코어링 미완이어도 트리거 — 실패는 stale 게이트
    로 기록되고 백오프 재시도가 08:50 스코어링 완료를 커버한다.

    ⚠️ 알려진 예외(트레이더 T3 Minor): 스코어링 succeeded 이력이 전무하면
    AnalysisService가 create_run 전에 반환해 실패 행이 안 남는다 —
    last_failure_at이 None으로 유지돼 백오프(300s) 대신 틱 간격(30s)으로
    재트리거된다. 게이트 조기 반환이라 LLM/뉴스 비용·브로커 호출이 없고
    방향도 "더 빠른 포착"이라 무해하나, Task 4 facts 구성은 "분석 실패 =
    항상 DB 행 존재"를 전제하지 말 것."""
    gate = _common_gate(facts.analyze, facts.paused)
    if gate is not None:
        return Decision(Job.ANALYZE, Action.WAIT, gate)
    if not trading_day:
        return Decision(Job.ANALYZE, Action.WAIT, Reason.NOT_TRADING_DAY)
    t = kst.time()
    if t < config.analyze_at:
        return Decision(Job.ANALYZE, Action.WAIT, Reason.WINDOW_NOT_OPEN)
    if t >= config.analyze_until:
        return _closed(Job.ANALYZE, facts.analyze)
    return _attempt(Job.ANALYZE, facts.analyze, kst,
                    config.analyze_retry_backoff_s)


def _eval_trade(kst: datetime, facts: TimelineFacts, config: ScheduleConfig,
                trading_day: bool) -> Decision:
    """트레이딩 start(결정 #39). 포기 없음 — 창(09:00~15:20) 내 60초
    백오프 무한 재시도(스펙 §5: 기동 실패 방치는 전일 보유 포지션의
    손절/감시 마비). 엔진 미조립은 SKIP(오늘 몫 실행 불가 확정)."""
    gate = _common_gate(facts.trade, facts.paused)
    if gate is not None:
        return Decision(Job.TRADE, Action.WAIT, gate)
    if not trading_day:
        return Decision(Job.TRADE, Action.WAIT, Reason.NOT_TRADING_DAY)
    if not facts.engine_assembled:
        return Decision(Job.TRADE, Action.SKIP, Reason.ENGINE_NOT_ASSEMBLED)
    t = kst.time()
    if t < config.trade_start_at:
        return Decision(Job.TRADE, Action.WAIT, Reason.WINDOW_NOT_OPEN)
    if t >= config.trade_until:
        return _closed(Job.TRADE, facts.trade)
    return _attempt(Job.TRADE, facts.trade, kst,
                    config.trade_retry_backoff_s)


def _next_trading_day(day: date, calendar) -> date:
    # 상한 30일(아키텍트 Minor — 결함 있는 calendar 주입이 항상 False를
    # 반환하면 무한 루프): KRX 최장 연휴(추석+주말)도 10일 미만.
    nxt = day + timedelta(days=1)
    for _ in range(30):
        if calendar.is_trading_day(nxt):
            return nxt
        nxt += timedelta(days=1)
    raise ValueError(
        f"no trading day within 30 days after {day} — calendar defective?")


def _previous_trading_day(day: date, calendar) -> date:
    prev = day - timedelta(days=1)
    for _ in range(30):
        if calendar.is_trading_day(prev):
            return prev
        prev -= timedelta(days=1)
    raise ValueError(
        f"no trading day within 30 days before {day} — calendar defective?")


def score_reference_for(now: datetime, config: ScheduleConfig,
                        calendar) -> date:
    """스코어링/수집 몫의 기준일 R 산정(스펙 §4-b 자정 경계, 순수 함수 —
    Task 4 store가 facts 구성 시 사용): 거래일 저녁(수집 창 개시 이후)이면
    오늘이 R, 그 외(아침·비거래일)는 직전 거래일. collect 몫과 score 몫은
    같은 R을 가리킨다 — 19:00 경계에서 R이 오늘로 넘어가는 순간 수집 몫도
    '오늘 미완'으로 바뀌어 트리거된다(창 열림과 동시)."""
    kst = now.astimezone(calendar.KST)
    today = kst.date()
    if calendar.is_trading_day(today) and kst.time() >= config.collect_at:
        return today
    return _previous_trading_day(today, calendar)


def evaluate(now: datetime, facts: TimelineFacts, config: ScheduleConfig,
             calendar) -> tuple[Decision, ...]:
    """4잡 전체 판정(잡당 정확히 1건). calendar는 is_trading_day/KST만
    소비(core.market_calendar 모듈 주입 — TradingService와 동일 관례)."""
    kst = now.astimezone(calendar.KST)
    trading_day = calendar.is_trading_day(kst.date())
    return (
        _eval_collect(kst, facts, config, trading_day),
        _eval_score(kst, facts, config, calendar),
        _eval_analyze(kst, facts, config, trading_day),
        _eval_trade(kst, facts, config, trading_day),
    )
