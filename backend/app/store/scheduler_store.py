"""스케줄러 저장 지원(P6 Task 4) — TimelineFacts의 DB 재료 구성 + 이벤트
적재(스펙 §6). 판정 질의는 **소유 스토어 헬퍼에 위임**한다(아키텍트 계획
리뷰 — 원시 SQL 4벌 복제 금지: 각 run 테이블 스키마 변경이 여기를 조용히
깨뜨리지 않게). 이 스토어의 쓰기는 scheduler_events뿐(insert-only).

running 플래그는 여기서 채우지 않는다 — at-most-one은 인프로세스 가드
(BackgroundRunService)가 진실이고 DB의 stale 'running' 행(크래시 잔재)은
완료도 실패도 아니므로, SchedulerService(Task 5)가 각 서비스의
is_running()으로 덧입힌다(dataclasses.replace)."""

import logging
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Protocol

from sqlalchemy import Engine, select
from sqlalchemy.orm import sessionmaker

from app.domain.orchestration.timeline import Action, Job, JobFacts, Reason
from app.store.models import SchedulerEventRow

logger = logging.getLogger(__name__)


class _DayJudged(Protocol):
    """소유 스토어 판정 헬퍼 공통 표면(P6 계획 Task 4 — 시그니처 통일)."""

    def has_completed_run(self, reference_date: date) -> bool: ...
    def last_failed_finished_at(self,
                                reference_date: date) -> datetime | None: ...


class _TradeJudged(Protocol):
    """트레이딩만 run_environment 편차(리플레이 교차 오염 차단 — §4-1).
    기본값 없음 — 생략 가능 신호를 인터페이스에서 제거(보안 T4:
    daily_order_usage의 "환경 인자 필수" 관례와 통일)."""

    def has_completed_run(self, reference_date: date,
                          run_environment: str) -> bool: ...
    def last_failed_finished_at(self, reference_date: date,
                                run_environment: str) -> datetime | None: ...


class SchedulerStore:
    def __init__(self, engine: Engine, collection: _DayJudged,
                 scoring: _DayJudged, analysis: _DayJudged,
                 trading: _TradeJudged, run_environment: str,
                 now: Callable[[], datetime] | None = None) -> None:
        self._sessions = sessionmaker(bind=engine)
        self._collection = collection
        self._scoring = scoring
        self._analysis = analysis
        self._trading = trading
        self._run_environment = run_environment
        self._now = now or (lambda: datetime.now(timezone.utc))

    def build_job_facts(self, reference: date,
                        today: date) -> dict[Job, JobFacts]:
        """잡별 JobFacts(running=False — 호출자가 is_running()으로 덧입힘).

        reference(R): 수집/스코어링 몫의 기준일(timeline.score_reference_for
        산정 — 저녁이면 오늘, 아침/비거래일이면 직전 거래일). collect 몫과
        score 몫이 같은 R을 가리키는 것이 §4-b 자정 경계 계약.
        today(E): 분석/트레이딩 몫의 날짜."""
        return {
            Job.COLLECT: JobFacts(
                completed=self._collection.has_completed_run(reference),
                last_failure_at=self._collection.last_failed_finished_at(
                    reference)),
            Job.SCORE: JobFacts(
                completed=self._scoring.has_completed_run(reference),
                last_failure_at=self._scoring.last_failed_finished_at(
                    reference)),
            Job.ANALYZE: JobFacts(
                completed=self._analysis.has_completed_run(today),
                last_failure_at=self._analysis.last_failed_finished_at(today)),
            Job.TRADE: JobFacts(
                completed=self._trading.has_completed_run(
                    today, self._run_environment),
                last_failure_at=self._trading.last_failed_finished_at(
                    today, self._run_environment)),
        }

    def record_event(self, job: Job, action: Action, reason: Reason,
                     run_id: int | None = None) -> None:
        """판정 이벤트 적재(결정 #36). enum 멤버만 수용 — 자유 텍스트가
        무인증 노출 표면(scheduler_events → /schedule/status)으로 새는 것을
        store 경계에서도 fail-loud 차단(Decision.__post_init__과 이중 방어,
        보안 T3). WAIT는 상태 서술이지 사건이 아니라 기록 거부."""
        if not isinstance(job, Job) or not isinstance(action, Action) \
                or not isinstance(reason, Reason):
            raise TypeError(
                "record_event accepts domain enum members only "
                f"(job={type(job).__name__}, action={type(action).__name__}, "
                f"reason={type(reason).__name__})")
        if action is Action.WAIT:
            raise ValueError("WAIT is a state, not an event — refuse to log")
        with self._sessions.begin() as session:
            session.add(SchedulerEventRow(
                ts=self._now(), job=job.value, action=action.value,
                reason=reason.value, run_id=run_id))

    def recent_events(self, limit: int = 20) -> list[dict]:
        """최근 이벤트(신규→과거) — /schedule/status 표면(Task 6)."""
        with self._sessions() as session:
            rows = session.scalars(
                select(SchedulerEventRow)
                .order_by(SchedulerEventRow.id.desc()).limit(limit)).all()
            return [{"ts": row.ts.isoformat(), "job": row.job,
                     "action": row.action, "reason": row.reason,
                     "run_id": row.run_id} for row in rows]
