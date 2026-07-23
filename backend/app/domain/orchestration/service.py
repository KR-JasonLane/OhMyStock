"""SchedulerService — 데일리 타임라인 틱 루프(P6 스펙 §5·§8).

BackgroundRunService **비상속**(스펙 §3): 그 베이스는 "1회 실행 run"의
스캐폴딩이고, 스케줄러는 프로세스 수명과 같이 사는 상주 루프라 모델이
다르다. lifespan(Task 6)이 start()로 기동하고 종료 시 태스크를 **가장
먼저** 취소한다(§8 셧다운 순서 — 정리 중인 서비스에 start() 재호출 방지).

주입 표면은 전부 로컬 Protocol이다 — 서비스 4종은 start()/is_running()만,
store는 build_job_facts()/record_event()만(아키텍트 계획 리뷰:
`app.store.*` 임포트는 타입힌트에도 금지, trading/service.py `_StoreLike`
선례). 판정은 timeline.evaluate(순수) 소관 — 이 클래스는 실행·이벤트
기록·수명주기만 담당한다."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from app.domain.orchestration.config import ScheduleConfig
from app.domain.orchestration.timeline import (Action, Decision, Job,
                                               JobFacts, Reason,
                                               TimelineFacts, evaluate,
                                               score_reference_for)

logger = logging.getLogger(__name__)


class _RunnableService(Protocol):
    """스케줄러가 소비하는 서비스 표면(BackgroundRunService 서브셋)."""

    def start(self) -> asyncio.Task | None: ...
    def is_running(self) -> bool: ...


class _SchedulerStoreLike(Protocol):
    """store.scheduler_store.SchedulerStore 표면(테스트 fake 계약)."""

    def build_job_facts(self, reference: date,
                        today: date) -> dict[Job, JobFacts]: ...
    def record_event(self, job: Job, action: Action, reason: Reason,
                     run_id: int | None = None) -> None: ...


class SchedulerService:
    def __init__(self, services: dict[Job, _RunnableService | None],
                 store: _SchedulerStoreLike, config: ScheduleConfig,
                 calendar, *,
                 sleep: Callable[[float], "asyncio.Future | object"] | None = None,
                 now: Callable[[], datetime] | None = None) -> None:
        """services: 잡별 서비스(트레이딩 미조립이면 None — facts의
        engine_assembled=False로 흘러 timeline이 SKIP 판정).
        calendar: core.market_calendar 모듈(주입 — timeline과 동일 관례)."""
        self._services = services
        self._store = store
        self._config = config
        self._calendar = calendar
        self._sleep = sleep or asyncio.sleep
        self._clock = now or (lambda: datetime.now(timezone.utc))
        self._paused = False
        self._dead = False
        self._restart_used = False
        self._task: asyncio.Task | None = None
        # 이벤트 중복 억제(스펙 §6 — 틱마다 같은 SKIP/GAVE_UP 재적재 방지):
        # 잡별 마지막 기록 (action, reason, 몫 날짜). TRIGGER/RETRY는 실행
        # 자체가 사건이라 dedup 없이 매번 기록(백오프가 자연 간격).
        # ⚠️ 인메모리 — 재기동 시 리셋돼 SKIP/GAVE_UP이 하루 몫당 최대 1건
        # 중복될 수 있다(수용: DB dedup은 틱마다 조회 부하가 더 크고,
        # append-only 감사 로그라 중복 1건이 판정을 오염하지 않는다 —
        # 아키텍트 T5).
        self._last_recorded: dict[Job, tuple[Action, Reason, date]] = {}
        self._last_decisions: dict[Job, Decision] = {}
        # 잡별 다음 시도 예정 시각(ISO) — /schedule/status의 "예정 시각"
        # 재료(스펙 §7, 아키텍트 T5 Important): 창 미개장이면 창 열림 시각,
        # 백오프 대기면 last_failure+backoff. 없으면 None.
        self._next_attempts: dict[Job, str | None] = {}

    # ── 수명주기 ────────────────────────────────────────────────────────

    def start(self) -> None:
        """lifespan 기동 훅 — 상주 루프 태스크 생성(1회)."""
        if self._task is not None:
            raise RuntimeError("scheduler already started")
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(self._on_task_done)

    def current_task(self) -> asyncio.Task | None:
        """lifespan 종료용 — 스케줄러 태스크를 **가장 먼저** 취소·await
        (스펙 §8: 정리 중인 서비스 start() 재호출/폐기된 엔진 질의 경합
        차단)."""
        return self._task

    def _on_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return  # 정상 셧다운 경로
        exc = task.exception()
        logger.critical("scheduler loop died: %s", type(exc).__name__,
                        exc_info=exc)
        if self._restart_used:
            # 예산 소진(스펙 §8 — **프로세스 수명당 총 1회**: 매 크래시
            # 1회로 읽으면 30초 간격 무한 크래시-재기동 루프). 회복 경로는
            # 컨테이너 재시작뿐 — /schedule/status가 dead를 영속 표시.
            self._dead = True
            logger.critical(
                "scheduler restart budget exhausted — scheduler is DEAD "
                "(automation stopped; restart the container to recover)")
            return
        self._restart_used = True
        logger.critical("scheduler restarting (budget: 1 per process life)")
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(self._on_task_done)

    # ── 운영 스위치 (Task 6 API 표면) ──────────────────────────────────

    def pause(self) -> None:
        """신규 트리거만 중단(실행 중 run은 영향 없음 — 스펙 §5). 인메모리
        — 재기동 시 enabled 복귀(§10-3, 영속 off는 SCHEDULER_ENABLED)."""
        self._paused = True
        logger.warning("scheduler paused (in-memory — resets on restart)")

    def resume(self) -> None:
        self._paused = False
        logger.info("scheduler resumed")

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def dead(self) -> bool:
        return self._dead

    def snapshot(self) -> dict:
        """/schedule/status 원천(Task 6) — 잡별 마지막 판정(고정 리터럴만)
        + 다음 시도 예정 시각(ISO — 스펙 §7 "예정 시각")."""
        return {
            "paused": self._paused,
            "dead": self._dead,
            "jobs": {job.value: {"action": d.action.value,
                                 "reason": d.reason.value,
                                 "next_attempt_at":
                                     self._next_attempts.get(job)}
                     for job, d in self._last_decisions.items()},
        }

    # ── 틱 루프 ────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise  # 셧다운 — 삼키면 취소가 전파되지 않는다
            except Exception:  # noqa: BLE001 — 틱 실패가 루프를 죽이지 않게
                # (facts 구성 등 틱 공통부 실패 — 다음 틱 재시도. 원문은
                # 로그 전용, 이벤트/status 유입 금지.) 로그는 의도적으로
                # 매 틱 반복 — 지속 장애(DB 다운 등)의 "지금도 진행 중"
                # 신호가 필요하고, 이 경로는 SKIP류와 달리 정상 운영에서
                # 발생하지 않아 노이즈 우려보다 관측성이 우선(개발자 T5).
                logger.exception("scheduler tick failed — will retry")
            await self._sleep(self._config.tick_interval_s)

    async def _tick(self) -> None:
        now = self._clock()
        today = now.astimezone(self._calendar.KST).date()
        reference = score_reference_for(now, self._config, self._calendar)
        facts = await asyncio.to_thread(self._store.build_job_facts,
                                        reference, today)
        facts = {job: replace(jf, running=self._is_running(job))
                 for job, jf in facts.items()}
        timeline_facts = TimelineFacts(
            collect=facts[Job.COLLECT], score=facts[Job.SCORE],
            analyze=facts[Job.ANALYZE], trade=facts[Job.TRADE],
            score_reference_date=reference,
            engine_assembled=self._services.get(Job.TRADE) is not None,
            paused=self._paused)
        for decision in evaluate(now, timeline_facts, self._config,
                                 self._calendar):
            self._last_decisions[decision.job] = decision
            try:
                # 힌트 계산도 격리 안에서(트레이더 T5 Minor — "1건 실패가
                # 나머지 잡을 안 막는다" 원칙의 완전 대칭)
                self._next_attempts[decision.job] = self._next_attempt_hint(
                    decision, facts[decision.job], today)
                await self._apply(decision, today)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 1건 실패가 나머지 잡을 안 막게
                logger.exception("scheduler decision failed: job=%s",
                                 decision.job.value)
                await self._record_once(decision.job, Action.START_REJECTED,
                                        Reason.EXECUTION_ERROR, today)

    def _is_running(self, job: Job) -> bool:
        svc = self._services.get(job)
        return svc is not None and svc.is_running()

    def _next_attempt_hint(self, decision: Decision, job_facts: JobFacts,
                           today: date) -> str | None:
        """다음 시도 예정 시각(ISO) — 상태 표시 전용(판정에 미사용).
        창 미개장 → 오늘 창 열림 시각(KST), 백오프 대기 → 실패+백오프."""
        if decision.reason is Reason.WINDOW_NOT_OPEN:
            opens = {Job.COLLECT: self._config.collect_at,
                     Job.ANALYZE: self._config.analyze_at,
                     Job.TRADE: self._config.trade_start_at}.get(decision.job)
            if opens is None:
                return None
            return datetime.combine(today, opens,
                                    tzinfo=self._calendar.KST).isoformat()
        if (decision.reason is Reason.RETRY_BACKOFF
                and job_facts.last_failure_at is not None):
            backoff = {Job.COLLECT: self._config.collect_retry_backoff_s,
                       Job.SCORE: self._config.score_retry_backoff_s,
                       Job.ANALYZE: self._config.analyze_retry_backoff_s,
                       Job.TRADE: self._config.trade_retry_backoff_s}
            stamp = job_facts.last_failure_at
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return (stamp + timedelta(
                seconds=backoff[decision.job])).astimezone(
                self._calendar.KST).isoformat()
        return None

    async def _apply(self, decision: Decision, today: date) -> None:
        job, action, reason = decision.job, decision.action, decision.reason
        if action is Action.WAIT:
            return
        if action in (Action.TRIGGER, Action.RETRY):
            svc = self._services[job]
            task = svc.start()
            if task is None:
                # 일시 상태 — 백오프 없이 다음 틱 재평가(스펙 §8). 사유
                # 구분(아키텍트 T5 Minor): facts.running 스냅샷 이후 수동
                # 트리거(API)가 같은 잡을 먼저 시작한 경합이면 자기-실행중
                # (ALREADY_RUNNING), 아니면 타 서비스 배타(CONFLICT).
                reason_now = (Reason.ALREADY_RUNNING if svc.is_running()
                              else Reason.CONFLICT)
                await self._record_once(job, Action.START_REJECTED,
                                        reason_now, today)
                return
            # run_id는 기록하지 않는다 — start()는 Task를 반환하고 run 행은
            # 그 태스크 안에서 비동기 생성되므로 트리거 시점엔 미확정.
            # 감사 조인은 ts 근접(같은 잡의 직후 run)으로 충분(스펙 §6은
            # run_id nullable).
            logger.info("scheduler decision job=%s action=%s reason=%s",
                        job.value, action.value, reason.value)
            await asyncio.to_thread(self._store.record_event, job, action,
                                    reason)
            return
        # SKIP/GAVE_UP — 상태 확정 서술: 이벤트·로그 모두 하루 몫당 1회
        await self._record_once(job, action, reason, today)

    async def _record_once(self, job: Job, action: Action, reason: Reason,
                           today: date) -> None:
        """상태 서술 이벤트(SKIP/GAVE_UP/START_REJECTED)의 하루-몫당 1회
        기록. **로그도 같은 dedup을 탄다**(개발자 T5 Important — DB만
        dedup하고 로그를 매 틱 찍으면 창 닫힌 뒤 다음 몫까지 최대 ~23시간
        동안 30초마다 동일 줄이 반복돼, "grep 한 줄로 하루 재구성"이라는
        결정 #36 목표가 노이즈에 묻힌다). 날짜가 키에 포함돼 자정(몫 전환)
        에 자연 리셋된다."""
        if self._last_recorded.get(job) == (action, reason, today):
            return
        level = logging.ERROR if action is Action.GAVE_UP else logging.WARNING
        logger.log(level, "scheduler decision job=%s action=%s reason=%s",
                   job.value, action.value, reason.value)
        await asyncio.to_thread(self._store.record_event, job, action, reason)
        self._last_recorded[job] = (action, reason, today)
