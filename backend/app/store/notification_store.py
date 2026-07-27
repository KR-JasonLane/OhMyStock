"""Append-only operational events and fixed-part notification delivery."""

import json
import logging
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Engine, and_, case, func, or_, select, update
from sqlalchemy.orm import aliased
from sqlalchemy.orm import sessionmaker

from app.core.market_calendar import KST
from app.domain.notifications.analysis_summary import (AnalysisVerdictSummary,
                                                        MorningAnalysisSummary)
from app.domain.notifications.models import NotificationPriority, OperationalEvent
from app.domain.notifications.digest import DigestSection
from app.store.kst_time import as_aware_utc, coarse_utc_bounds, within_kst_day
from app.store.models import (AnalysisRunRow, AnalysisVerdictRow, CollectionRunRow,
                              InstrumentRow,
                              NotificationDeliveryRow, NotificationOutboxRow,
                              OperationalEventRow, SchedulerEventRow, ScoreRunRow,
                              TelegramStateRow, TradeOrderRow, TradePositionRow,
                              TradeRunRow)
from app.store.telegram_common import (MAX_DELIVERY_PARTS,
                                       MAX_DELIVERY_TOTAL_BYTES,
                                       aware as _aware, canonical_json,
                                       delivery_body, exact_nonnegative_int,
                                       identifier, positive_int, safe_identifier)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Delivery:
    id: int
    outbox_id: int
    part_index: int
    total_parts: int
    body: str | None
    status: str
    attempt_count: int
    last_error_kind: str | None
    version: int
    priority: int
    occurred_at: datetime
    created_at: datetime


@dataclass(frozen=True)
class MaterializedNotification:
    """One durable notification outbox materialization result."""

    outbox_id: int
    created: bool
    priority: NotificationPriority


class AnalysisSummaryRunStore:
    """성공한 분석 결과를 알림 허용목록 DTO로만 읽는 SQL read model."""

    def __init__(self, engine: Engine, run_environment: str,
                 now: Callable[[], datetime] | None = None) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self._run_environment = run_environment
        self._now = now or (lambda: datetime.now(timezone.utc))

    def pending_succeeded_today(
            self, generated_run_ids: Collection[int], limit: int = 10,
    ) -> tuple[MorningAnalysisSummary, ...]:
        """당일 KST에 시작한 미생성 성공 분석을 id 오름차순으로 반환한다."""
        limit = positive_int(limit, "limit")
        generated = {
            run_id for run_id in generated_run_ids
            if type(run_id) is int and run_id > 0
        }
        now = as_aware_utc(self._now())
        today = now.astimezone(KST).date()
        lo, hi = coarse_utc_bounds(today)
        with self._sessions() as session:
            statement = select(AnalysisRunRow, ScoreRunRow.reference_date).join(
                ScoreRunRow, AnalysisRunRow.score_run_id == ScoreRunRow.id,
            ).where(
                AnalysisRunRow.status == "succeeded",
                AnalysisRunRow.finished_at.is_not(None),
                AnalysisRunRow.started_at >= lo,
                AnalysisRunRow.started_at <= hi,
            )
            if generated:
                statement = statement.where(~AnalysisRunRow.id.in_(generated))
            # coarse_utc_bounds는 ±1일의 SQL prefilter다. 여기서 LIMIT를 먼저
            # 적용하면 전일/익일 후보가 당일 run을 밀어내므로, 정확 KST 판정 뒤
            # 서비스 tick 상한을 적용한다.
            runs = session.execute(statement.order_by(AnalysisRunRow.id)).all()
            summaries = [
                summary for run, reference_day in runs
                if within_kst_day(run.started_at, today)
                if (summary := self._summary_for_run(session, run, reference_day)) is not None
            ]
        return tuple(summaries[:limit])

    def latest_analysis(self) -> MorningAnalysisSummary | None:
        """가장 최근 성공 분석만 읽는다. 환경은 DB 필터가 아닌 설정 값이다."""
        with self._sessions() as session:
            row = session.execute(select(
                AnalysisRunRow, ScoreRunRow.reference_date,
            ).join(
                ScoreRunRow, AnalysisRunRow.score_run_id == ScoreRunRow.id,
            ).where(
                AnalysisRunRow.status == "succeeded",
                AnalysisRunRow.finished_at.is_not(None),
            ).order_by(
                AnalysisRunRow.finished_at.desc(), AnalysisRunRow.id.desc(),
            ).limit(1)).first()
            if row is None:
                return None
            run, reference_day = row
            return self._summary_for_run(session, run, reference_day)

    def _summary_for_run(self, session, run: AnalysisRunRow,
                         reference_day: date) -> MorningAnalysisSummary | None:
        rows = session.execute(select(
            AnalysisVerdictRow, InstrumentRow.name,
        ).outerjoin(
            InstrumentRow, InstrumentRow.symbol == AnalysisVerdictRow.symbol,
        ).where(
            AnalysisVerdictRow.run_id == run.id,
        ).order_by(AnalysisVerdictRow.symbol)).all()
        if len(rows) > 20:
            # 어느 20행을 남길지 정하면 DB 정렬 순서가 분석 판단을 바꾼다.
            # 상한 위반 run의 verdict 전체를 격리해 후보를 만들어 내지 않는다.
            try:
                return self._make_summary(run, reference_day, (), len(rows))
            except ValueError:
                logger.warning("analysis summary read skipped corrupt run %d", run.id)
                return None

        corrupted_rows = 0
        verdicts: list[AnalysisVerdictSummary] = []
        for verdict, name in rows:
            reasons, bad_reasons = _json_string_tuple(verdict.reasons)
            risk_flags, bad_risk_flags = _json_string_tuple(verdict.risk_flags)
            try:
                verdicts.append(AnalysisVerdictSummary(
                    symbol=verdict.symbol, name=name, verdict=verdict.verdict,
                    confidence=verdict.confidence, reasons=reasons,
                    risk_flags=risk_flags, picked=verdict.picked,
                    pick_rank=verdict.pick_rank))
            except ValueError:
                corrupted_rows += 1
                continue
            corrupted_rows += int(bad_reasons or bad_risk_flags)

        try:
            return self._make_summary(run, reference_day, tuple(verdicts), corrupted_rows)
        except ValueError:
            # pick_rank의 gap/중복, pick 상한 초과는 어느 행 하나를 고쳐서
            # 안전하게 해석할 수 없다. 최종 후보 전체를 보수적으로 격리한다.
            non_picks = tuple(verdict for verdict in verdicts if not verdict.picked)
            corrupted_rows += len(verdicts) - len(non_picks)
            try:
                return self._make_summary(run, reference_day, non_picks, corrupted_rows)
            except ValueError:
                logger.warning("analysis summary read skipped corrupt run %d", run.id)
                return None

    def _make_summary(self, run: AnalysisRunRow, reference_day: date,
                      verdicts: tuple[AnalysisVerdictSummary, ...],
                      corrupted_rows: int) -> MorningAnalysisSummary:
        return MorningAnalysisSummary(
            run_id=run.id, run_environment=self._run_environment,
            regime=run.regime, market_summary=run.market_summary,
            max_picks_advice=run.max_picks_advice,
            score_reference_date=reference_day,
            started_at=as_aware_utc(run.started_at),
            finished_at=as_aware_utc(run.finished_at), verdicts=verdicts,
            corrupted_rows=corrupted_rows)


def _json_string_tuple(value: str | bytes) -> tuple[tuple[str, ...], bool]:
    """DB JSON은 string array만 허용한다. 그 외는 안전한 빈 값으로 격리한다."""
    try:
        decoded = json.loads(value)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return (), True
    if not isinstance(decoded, list) or not all(type(item) is str for item in decoded):
        return (), True
    return tuple(decoded), False


class DigestRunStore:
    """기존 run 테이블을 다이제스트 허용목록 DTO로만 읽는 read model."""

    def __init__(self, engine: Engine, run_environment: str,
                 now: Callable[[], datetime] | None = None) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self._run_environment = run_environment
        self._now = now or (lambda: datetime.now(timezone.utc))

    def pipeline_summary(self, trading_day: date) -> DigestSection:
        with self._sessions() as session:
            analysis = self._latest_started(AnalysisRunRow, trading_day, session=session)
            score = (session.get(ScoreRunRow, analysis.score_run_id)
                     if analysis is not None else None)
            pick_count = 0
            if analysis is not None:
                pick_count = session.scalar(select(func.count()).select_from(
                    AnalysisVerdictRow).where(
                    AnalysisVerdictRow.run_id == analysis.id,
                    AnalysisVerdictRow.picked.is_(True))) or 0
        reference_day = score.reference_date if score is not None else None
        collection = (self._latest_started(CollectionRunRow, reference_day)
                      if reference_day is not None else None)
        facts = {
            "collection_status": collection.status if collection else "unavailable",
            "collection_reference_day": (reference_day.isoformat()
                                         if reference_day else None),
            "scoring_status": score.status if score else "unavailable",
            "scoring_reference_day": (score.reference_date.isoformat()
                                        if score else None),
            "candidate_count": score.universe_count if score else None,
            "analysis_status": analysis.status if analysis else "unavailable",
            "analysis_score_reference_day": (reference_day.isoformat()
                                               if reference_day else None),
            "pick_count": pick_count if analysis else None,
            "market_regime": analysis.regime if analysis else None,
        }
        rows = (collection, score, analysis)
        return DigestSection(
            facts, _latest_finished_or_started(rows),
            tuple(name for name, row in zip(
                ("collection", "scoring", "analysis"), rows) if row is None))

    def trading_summary(self, trading_day: date) -> DigestSection:
        runs = self._started_on(TradeRunRow, trading_day,
                                TradeRunRow.run_environment == self._run_environment)
        run_ids = [row.id for row in runs]
        with self._sessions() as session:
            if run_ids:
                orders = session.scalars(select(TradeOrderRow).where(
                    TradeOrderRow.trade_run_id.in_(run_ids))).all()
            else:
                orders = ()
            lo, hi = coarse_utc_bounds(trading_day)
            closed = session.scalars(select(TradePositionRow).join(TradeRunRow).where(
                TradeRunRow.run_environment == self._run_environment,
                TradePositionRow.realized_pnl.is_not(None),
                TradePositionRow.closed_at.is_not(None), TradePositionRow.closed_at >= lo,
                TradePositionRow.closed_at <= hi)).all()
            closed = [row for row in closed
                      if within_kst_day(row.closed_at, trading_day)]
            open_count = session.scalar(select(func.count()).select_from(
                TradePositionRow).join(TradeRunRow).where(
                TradeRunRow.run_environment == self._run_environment,
                TradePositionRow.state.in_(("entered", "exiting")))) or 0
            scheduler_events = session.scalars(select(SchedulerEventRow).where(
                SchedulerEventRow.action == "gave_up", SchedulerEventRow.ts >= lo,
                SchedulerEventRow.ts <= hi)).all()
            gave_up_count = sum(within_kst_day(event.ts, trading_day)
                                for event in scheduler_events)
            dead_events = session.scalars(select(OperationalEventRow).where(
                OperationalEventRow.kind == "scheduler_dead",
                OperationalEventRow.occurred_at >= lo,
                OperationalEventRow.occurred_at <= hi)).all()
            scheduler_dead_count = sum(within_kst_day(event.occurred_at, trading_day)
                                       for event in dead_events)
            dead_letters = session.scalars(select(NotificationOutboxRow).where(
                NotificationOutboxRow.status == "dead_letter",
                NotificationOutboxRow.occurred_at >= lo,
                NotificationOutboxRow.occurred_at <= hi)).all()
            dead_letter_count = sum(within_kst_day(row.occurred_at, trading_day)
                                    for row in dead_letters)
        buy_count = sum(order.side == "buy" for order in orders)
        sell_count = sum(order.side == "sell" for order in orders)
        return DigestSection({
            "order_count": len(orders), "entry_order_count": buy_count,
            "exit_order_count": sell_count, "current_position_count": open_count,
            "realized_pnl": sum(row.realized_pnl or 0 for row in closed),
            "realized_pnl_confidence": "estimated",
            "kill_switch_run_count": sum(row.stopped_by_kill_switch for row in runs),
            "scheduler_gave_up_count": gave_up_count,
            "scheduler_dead_count": scheduler_dead_count,
            "dead_letter_count": dead_letter_count,
        }, self._now(), ("trade_runs",) if not runs else ())

    def _latest_started(self, model, trading_day: date, *, session=None):
        rows = self._started_on(model, trading_day, session=session)
        return max(rows, key=lambda row: row.started_at, default=None)

    def _started_on(self, model, trading_day: date, *conditions, session=None):
        lo, hi = coarse_utc_bounds(trading_day)
        own_session = session is None
        session = session or self._sessions()
        try:
            rows = session.scalars(select(model).where(
                *conditions, model.started_at >= lo, model.started_at <= hi)).all()
            return [row for row in rows if within_kst_day(row.started_at, trading_day)]
        finally:
            if own_session:
                session.close()


class NotificationStore:
    # Sender total request deadline(30s) + cancellation/DB handoff margin(5s).
    SENSITIVE_DELIVERY_MIN_TTL_S = 35

    def __init__(self, engine: Engine, now: Callable[[], datetime] | None = None) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self._now = now or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _insert_outbox(session, values: dict[str, Any]) -> tuple[int, bool]:
        dialect = session.bind.dialect.name
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
            statement = insert(NotificationOutboxRow).values(**values).on_conflict_do_nothing(
                index_elements=["idempotency_key"]).returning(NotificationOutboxRow.id)
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
            statement = insert(NotificationOutboxRow).values(**values).on_conflict_do_nothing(
                index_elements=["idempotency_key"]).returning(NotificationOutboxRow.id)
        else:
            row = NotificationOutboxRow(**values)
            session.add(row)
            session.flush()
            return row.id, True
        inserted = session.execute(statement).scalar_one_or_none()
        if inserted is not None:
            return inserted, True
        existing = session.scalar(select(NotificationOutboxRow.id).where(
            NotificationOutboxRow.idempotency_key == values["idempotency_key"]))
        return existing, False

    def append_event(self, event: OperationalEvent) -> int | None:
        with self._sessions.begin() as session:
            return self.append_event_in_session(session, event)

    def append_event_in_session(self, session, event: OperationalEvent) -> int:
        _aware(event.occurred_at)
        row = OperationalEventRow(
            kind=event.kind, source_type=event.source_type,
            source_id=event.source_id, source_version=event.version,
            payload=canonical_json(event.payload_for_storage()),
            occurred_at=event.occurred_at)
        values = {
            "kind": row.kind, "source_type": row.source_type,
            "source_id": row.source_id, "source_version": row.source_version,
            "payload": row.payload, "occurred_at": row.occurred_at,
        }
        dialect = session.bind.dialect.name
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
            session.execute(insert(OperationalEventRow).values(**values).on_conflict_do_nothing(
                index_elements=["source_type", "source_id", "source_version"]))
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
            session.execute(insert(OperationalEventRow).values(**values).on_conflict_do_nothing(
                index_elements=["source_type", "source_id", "source_version"]))
        else:
            session.add(row)
            session.flush()
        return session.scalar(select(OperationalEventRow.id).where(
                OperationalEventRow.source_type == event.source_type,
                OperationalEventRow.source_id == event.source_id,
                OperationalEventRow.source_version == event.version))

    def operational_event_count(self) -> int:
        with self._sessions() as session:
            return session.scalar(select(func.count()).select_from(OperationalEventRow)) or 0

    def latest_operational_event(self) -> OperationalEvent:
        with self._sessions() as session:
            row = session.scalar(select(OperationalEventRow).order_by(
                OperationalEventRow.id.desc()).limit(1))
            if row is None:
                raise LookupError("no operational event")
            occurred_at = row.occurred_at
            if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
                occurred_at = occurred_at.replace(tzinfo=timezone.utc)
            return OperationalEvent(
                kind=row.kind, source_type=row.source_type, source_id=row.source_id,
                version=row.source_version, payload=json.loads(row.payload),
                occurred_at=occurred_at)

    def events_after_in_session(self, session, event_id: int,
                                limit: int) -> list[OperationalEventRow]:
        return list(session.scalars(
            select(OperationalEventRow)
            .where(OperationalEventRow.id > event_id)
            .order_by(OperationalEventRow.id)
            .limit(limit)))

    def projector_checkpoint_in_session(self, session) -> int:
        row = session.get(TelegramStateRow, "event_projector_cursor")
        return int(row.value) if row is not None else 0

    def projector_checkpoint(self) -> int:
        with self._sessions() as session:
            return self.projector_checkpoint_in_session(session)

    def set_projector_checkpoint_in_session(self, session, event_id: int) -> None:
        row = session.get(TelegramStateRow, "event_projector_cursor")
        if row is None:
            session.add(TelegramStateRow(
                key="event_projector_cursor", value=str(event_id),
                lease_owner=None, lease_until=None, updated_at=_aware(self._now())))
            return
        row.value = str(event_id)
        row.updated_at = _aware(self._now())

    def rewind_projector_checkpoint(self, event_id: int) -> None:
        if event_id < 0:
            raise ValueError("event_id must be non-negative")
        with self._sessions.begin() as session:
            self.set_projector_checkpoint_in_session(session, event_id)

    def enqueue_outbox_in_session(
            self, session, idempotency_key: str, payload: Any, *, kind: str,
            priority: int = 0, occurred_at: datetime,
            bodies: Sequence[str] = ()) -> tuple[int, bool]:
        identifier(idempotency_key, "idempotency_key", 128)
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            # SQLite는 timezone=True 값을 naive로 되돌린다. 원천 행은 UTC로
            # 기록하므로 projector 경계에서 UTC를 복원한다.
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        occurred = _aware(occurred_at)
        now = _aware(self._now())
        checked = self._checked_delivery_bodies(bodies, priority)
        outbox_id, created = self._insert_outbox(session, dict(
            idempotency_key=idempotency_key, kind=kind, priority=priority,
            sensitive=False, payload=canonical_json(payload), status="pending",
            next_attempt_at=now, occurred_at=occurred, created_at=now,
            sent_at=None, last_error_kind=None, retention_kind="standard",
            purge_at=None))
        if created:
            self._add_delivery_rows(session, outbox_id, checked, now)
        return outbox_id, created

    def project_operational_events(self, project, limit: int = 100) -> int:
        """project(event_id, OperationalEvent)의 outbox와 cursor를 원자 커밋.

        투영 정책은 domain callback으로 남기고, SQLAlchemy session과 ORM 행은
        store에 가둔다. callback이 예외를 내면 outbox와 checkpoint 모두 rollback된다.
        """
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._sessions.begin() as session:
            cursor = self.projector_checkpoint_in_session(session)
            rows = self.events_after_in_session(session, cursor, limit)
            inserted = 0
            for row in rows:
                occurred_at = row.occurred_at
                if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
                    occurred_at = occurred_at.replace(tzinfo=timezone.utc)
                event = OperationalEvent(
                    kind=row.kind, source_type=row.source_type, source_id=row.source_id,
                    version=row.source_version, payload=json.loads(row.payload),
                    occurred_at=occurred_at)
                for projection in project(row.id, event):
                    _outbox_id, created = self.enqueue_outbox_in_session(
                        session, projection.idempotency_key, projection.payload,
                        kind=projection.kind, priority=projection.priority,
                        occurred_at=event.occurred_at,
                        bodies=tuple(part.text for part in projection.parts))
                    inserted += int(created)
                self.set_projector_checkpoint_in_session(session, row.id)
            return inserted

    def outbox_count_by_key(self, idempotency_key: str) -> int:
        with self._sessions() as session:
            return session.scalar(select(func.count()).select_from(
                NotificationOutboxRow).where(
                NotificationOutboxRow.idempotency_key == idempotency_key)) or 0

    def generated_digest_days(self, run_environment: str) -> tuple[date, ...]:
        """Outbox가 존재한 날짜는 delivery 상태와 무관하게 생성 완료다."""
        prefix = f"digest:{run_environment}:"
        with self._sessions() as session:
            keys = session.scalars(select(NotificationOutboxRow.idempotency_key).where(
                NotificationOutboxRow.idempotency_key.like(f"{prefix}%"))).all()
        days: set[date] = set()
        for key in keys:
            suffix = key.removeprefix(prefix)
            try:
                days.add(date.fromisoformat(suffix))
            except ValueError:
                # 다른 producer의 malformed key는 scheduler를 막지 않는다.
                continue
        return tuple(sorted(days))

    def generated_analysis_run_ids(self, run_environment: str) -> tuple[int, ...]:
        """생성된 분석 outbox id는 delivery 상태와 무관하게 완료 이력이다."""
        prefix = f"analysis-summary:{run_environment}:"
        with self._sessions() as session:
            keys = session.scalars(select(NotificationOutboxRow.idempotency_key).where(
                NotificationOutboxRow.idempotency_key.like(f"{prefix}%"))).all()
        run_ids: set[int] = set()
        for key in keys:
            suffix = key.removeprefix(prefix)
            if suffix.isdecimal() and (run_id := int(suffix)) > 0:
                run_ids.add(run_id)
        return tuple(sorted(run_ids))

    def record_digest_skipped_stale(self, trading_day: date,
                                    run_environment: str, now: datetime) -> None:
        """7 거래일 window 밖 누락을 재생성하지 않고 audit으로 종결한다."""
        self.append_event(OperationalEvent(
            kind="digest_skipped_stale", source_type="digest",
            source_id=trading_day.toordinal(), version=f"stale-v1:{run_environment}",
            payload={"trading_day": trading_day.isoformat(),
                     "run_environment": run_environment}, occurred_at=_aware(now)))

    def materialize_digest(self, idempotency_key: str, payload: Any,
                           bodies: Sequence[str] | str,
                           *, occurred_at: datetime) -> MaterializedNotification:
        """Digest payload와 고정 delivery body를 한 transaction에 생성한다."""
        identifier(idempotency_key, "idempotency_key", 128)
        if not idempotency_key.startswith("digest:"):
            raise ValueError("digest idempotency_key must start with digest:")
        occurred = _aware(occurred_at)
        now = _aware(self._now())
        if isinstance(bodies, str):
            bodies = (bodies,)
        if not bodies:
            raise ValueError("digest requires at least one delivery body")
        if len(bodies) > MAX_DELIVERY_PARTS:
            raise ValueError("digest delivery part count exceeds 64")
        checked = self._checked_delivery_bodies(bodies, NotificationPriority.DIGEST)
        with self._sessions.begin() as session:
            outbox_id, created = self._insert_outbox(session, dict(
                idempotency_key=idempotency_key, kind="digest",
                priority=NotificationPriority.DIGEST, sensitive=True,
                payload=canonical_json(payload), status="pending",
                next_attempt_at=now, occurred_at=occurred, created_at=now,
                sent_at=None, last_error_kind=None, retention_kind="digest",
                purge_at=now + timedelta(hours=24)))
            if created:
                self._add_delivery_rows(session, outbox_id, checked, now)
            return MaterializedNotification(
                outbox_id, created, NotificationPriority.DIGEST)

    def materialize_analysis_summary(
            self, summary: MorningAnalysisSummary, bodies: Sequence[str], *,
            occurred_at: datetime) -> MaterializedNotification:
        """분석 허용목록 payload와 delivery parts를 하나의 transaction에 만든다."""
        if isinstance(bodies, str) or not bodies:
            raise ValueError("analysis summary requires at least one delivery body")
        if len(bodies) > MAX_DELIVERY_PARTS:
            raise ValueError("analysis summary delivery part count exceeds 64")
        occurred = _aware(occurred_at)
        now = _aware(self._now())
        checked = self._checked_delivery_bodies(bodies, NotificationPriority.NORMAL)
        payload = {
            "version": 1,
            "analysis_run_id": summary.run_id,
            "run_environment": summary.run_environment,
            "score_reference_date": summary.score_reference_date.isoformat(),
        }
        with self._sessions.begin() as session:
            outbox_id, created = self._insert_outbox(session, dict(
                idempotency_key=summary.idempotency_key, kind="analysis_summary",
                priority=NotificationPriority.NORMAL, sensitive=True,
                payload=canonical_json(payload), status="pending",
                next_attempt_at=now, occurred_at=occurred, created_at=now,
                sent_at=None, last_error_kind=None, retention_kind="digest",
                purge_at=now + timedelta(hours=24)))
            if created:
                self._add_delivery_rows(session, outbox_id, checked, now)
            return MaterializedNotification(
                outbox_id, created, NotificationPriority.NORMAL)

    def materialize_missing_operational_deliveries(
            self, render: Callable[[str, dict[str, object]], tuple[str, ...]],
            limit: int = 100) -> int:
        """Recover Task-6-era operational outboxes that predate child rows."""
        limit = positive_int(limit, "limit")
        materialized = 0
        with self._sessions.begin() as session:
            child = aliased(NotificationDeliveryRow)
            rows = session.scalars(select(NotificationOutboxRow).where(
                NotificationOutboxRow.status == "pending",
                NotificationOutboxRow.idempotency_key.like("operational:%"),
                ~select(child.id).where(child.outbox_id == NotificationOutboxRow.id).exists()
            ).order_by(NotificationOutboxRow.id).limit(limit)).all()
            for row in rows:
                outbox = session.scalar(select(NotificationOutboxRow).where(
                    NotificationOutboxRow.id == row.id).with_for_update())
                if outbox is None or outbox.status != "pending":
                    continue
                exists = session.scalar(select(NotificationDeliveryRow.id).where(
                    NotificationDeliveryRow.outbox_id == outbox.id).limit(1))
                if exists is not None or outbox.payload is None:
                    continue
                payload = json.loads(outbox.payload)
                if not isinstance(payload, dict):
                    continue
                try:
                    bodies = self._checked_delivery_bodies(
                        render(outbox.kind, payload), outbox.priority)
                except ValueError:
                    # Legacy outboxes could contain a formerly accepted but now
                    # unsafe kind.  Preserve metadata for audit, stop retrying,
                    # and never turn that raw identifier into Telegram text.
                    outbox.status = "dead_letter"
                    outbox.last_error_kind = "unsupported_notification_kind"
                    continue
                self._add_delivery_rows(session, outbox.id, bodies, _aware(self._now()))
                materialized += 1
            return materialized

    def enqueue_outbox(self, idempotency_key: str, payload: Any, kind: str = "generic",
                       priority: int = 0, sensitive: bool = False,
                       occurred_at: datetime | None = None,
                       retention_kind: str = "standard") -> int:
        now = _aware(self._now())
        occurred = _aware(occurred_at) if occurred_at else now
        identifier(idempotency_key, "idempotency_key", 128)
        if retention_kind not in {"standard", "query", "digest"}:
            raise ValueError("invalid retention_kind")
        if sensitive and retention_kind == "standard":
            raise ValueError("sensitive outbox requires query or digest retention")
        purge_at = (now + timedelta(minutes=15) if retention_kind == "query"
                    else now + timedelta(hours=24) if retention_kind == "digest"
                    else None)
        with self._sessions.begin() as session:
            values = dict(
                idempotency_key=idempotency_key, kind=kind, priority=priority,
                sensitive=sensitive, payload=canonical_json(payload), status="pending",
                next_attempt_at=now, occurred_at=occurred, created_at=now,
                sent_at=None, last_error_kind=None, retention_kind=retention_kind,
                purge_at=purge_at)
            return self._insert_outbox(session, values)[0]

    def count_outbox(self) -> int:
        with self._sessions() as session:
            return session.scalar(select(func.count()).select_from(NotificationOutboxRow)) or 0

    def delivery_counts(self) -> dict[str, int]:
        """Aggregate delivery states without loading message bodies."""
        snapshot = self.delivery_state_snapshot()
        return {
            key: int(snapshot[key])
            for key in ("pending", "sending", "sent", "dead_letter")
        }

    def delivery_state_snapshot(self) -> dict[str, int | str | None]:
        """Aggregate durable retry state without exposing error text or IDs."""
        with self._sessions() as session:
            retry = and_(
                NotificationDeliveryRow.status == "pending",
                NotificationDeliveryRow.attempt_count > 0,
            )
            (
                pending,
                sending,
                sent,
                dead_letter,
                retry_pending,
                rate_limited,
                send_deadline,
            ) = session.execute(
                select(
                    *(
                        func.coalesce(func.sum(case(
                            (
                                NotificationDeliveryRow.status == status,
                                1,
                            ),
                            else_=0,
                        )), 0)
                        for status in (
                            "pending", "sending", "sent", "dead_letter"
                        )
                    ),
                    func.coalesce(func.sum(case(
                        (retry, 1), else_=0
                    )), 0),
                    func.coalesce(func.sum(case(
                        (
                            and_(
                                retry,
                                or_(
                                    NotificationDeliveryRow.last_http_status
                                    == 429,
                                    NotificationDeliveryRow.last_error_kind
                                    == "rate_limited",
                                ),
                            ),
                            1,
                        ),
                        else_=0,
                    )), 0),
                    func.coalesce(func.sum(case(
                        (
                            and_(
                                retry,
                                NotificationDeliveryRow.last_error_kind
                                == "send_deadline",
                            ),
                            1,
                        ),
                        else_=0,
                    )), 0),
                )
            ).one()
        backoff_reason: str | None = None
        if retry_pending > 0:
            if rate_limited > 0:
                backoff_reason = "rate_limited"
            elif send_deadline > 0:
                backoff_reason = "send_deadline"
            else:
                backoff_reason = "temporary_error"
        return {
            "pending": pending,
            "sending": sending,
            "sent": sent,
            "dead_letter": dead_letter,
            "retry_pending": retry_pending,
            "backoff_reason": backoff_reason,
        }

    def enqueue_parts(self, idempotency_key: str, bodies: Sequence[str],
                      sensitive: bool = False, payload: Any | None = None,
                      retention_kind: str | None = None, priority: int = 10,
                      occurred_at: datetime | None = None) -> int:
        now = _aware(self._now())
        occurred = _aware(occurred_at) if occurred_at else now
        if not bodies:
            raise ValueError("at least one delivery part is required")
        if len(bodies) > MAX_DELIVERY_PARTS:
            raise ValueError("delivery part count exceeds 64")
        identifier(idempotency_key, "idempotency_key", 128)
        checked = self._checked_delivery_bodies(bodies, priority)
        retention_kind = retention_kind or ("query" if sensitive else "standard")
        if retention_kind not in {"standard", "query", "digest"}:
            raise ValueError("invalid retention_kind")
        if sensitive and retention_kind == "standard":
            raise ValueError("sensitive outbox requires query or digest retention")
        purge_at = (now + timedelta(minutes=15) if retention_kind == "query"
                    else now + timedelta(hours=24) if retention_kind == "digest"
                    else None)
        with self._sessions.begin() as session:
            values = dict(
                idempotency_key=idempotency_key, kind="generic", priority=priority,
                sensitive=sensitive, payload=canonical_json(payload if payload is not None else {}),
                status="pending", next_attempt_at=now, occurred_at=occurred,
                created_at=now, sent_at=None, last_error_kind=None,
                retention_kind=retention_kind, purge_at=purge_at)
            outbox_id, created = self._insert_outbox(session, values)
            if not created:
                return outbox_id
            self._add_delivery_rows(session, outbox_id, checked, now)
            return outbox_id

    @staticmethod
    def _checked_delivery_bodies(bodies: Sequence[str], priority: int) -> list[str]:
        checked = [delivery_body(body) for body in bodies]
        if priority == 0 and any(len(body) > 4032 for body in checked):
            raise ValueError("critical delivery body must reserve delayed notice space")
        if sum(len(body.encode("utf-8")) for body in checked) > MAX_DELIVERY_TOTAL_BYTES:
            raise ValueError("delivery total exceeds 256 KiB")
        return checked

    @staticmethod
    def _add_delivery_rows(session, outbox_id: int, bodies: Sequence[str], now: datetime) -> None:
        total = len(bodies)
        for index, body in enumerate(bodies, start=1):
            session.add(NotificationDeliveryRow(
                outbox_id=outbox_id, part_index=index, total_parts=total,
                body=body, status="pending", owner=None, lease_until=None,
                attempt_count=0, created_at=now, first_attempt_at=None,
                next_attempt_at=now, sent_at=None, last_error_kind=None,
                last_http_status=None, telegram_message_id=None, version=0))

    def claim_deliveries(self, owner: str, limit: int = 100,
                         lease_s: int = 30) -> list[Delivery]:
        owner = safe_identifier(owner, "owner")
        limit = positive_int(limit, "limit")
        lease_s = positive_int(lease_s, "lease_s")
        now = _aware(self._now())
        send_deadline = now + timedelta(seconds=self.SENSITIVE_DELIVERY_MIN_TTL_S)
        until = now + timedelta(seconds=lease_s)
        claimed: list[Delivery] = []
        with self._sessions.begin() as session:
            sibling = aliased(NotificationDeliveryRow)
            rows = session.scalars(select(NotificationDeliveryRow).join(
                NotificationOutboxRow,
                NotificationOutboxRow.id == NotificationDeliveryRow.outbox_id).where(
                NotificationOutboxRow.status == "pending",
                NotificationDeliveryRow.next_attempt_at <= now,
                or_(NotificationOutboxRow.purge_at.is_(None),
                    NotificationOutboxRow.purge_at > send_deadline),
                or_(NotificationDeliveryRow.status == "pending",
                    (NotificationDeliveryRow.status == "sending")
                    & (NotificationDeliveryRow.lease_until < now)),
                ~select(sibling.id).where(
                    sibling.outbox_id == NotificationDeliveryRow.outbox_id,
                    sibling.part_index < NotificationDeliveryRow.part_index,
                    sibling.status != "sent").exists(),
                ~select(sibling.id).where(
                    sibling.outbox_id == NotificationDeliveryRow.outbox_id,
                    sibling.id != NotificationDeliveryRow.id,
                    sibling.status == "sending",
                    sibling.lease_until >= now).exists())
                .order_by(NotificationOutboxRow.priority,
                          NotificationOutboxRow.occurred_at,
                          NotificationDeliveryRow.part_index).limit(limit)).all()
            for row in rows:
                outbox = session.scalar(select(NotificationOutboxRow).where(
                    NotificationOutboxRow.id == row.outbox_id).with_for_update())
                if outbox is None or outbox.status != "pending":
                    continue
                active_sibling = session.scalar(select(NotificationDeliveryRow.id).where(
                    NotificationDeliveryRow.outbox_id == row.outbox_id,
                    NotificationDeliveryRow.id != row.id,
                    NotificationDeliveryRow.status == "sending",
                    NotificationDeliveryRow.lease_until >= now).limit(1))
                if active_sibling is not None:
                    continue
                earlier_unsent = session.scalar(select(NotificationDeliveryRow.id).where(
                    NotificationDeliveryRow.outbox_id == row.outbox_id,
                    NotificationDeliveryRow.part_index < row.part_index,
                    NotificationDeliveryRow.status != "sent").limit(1))
                if earlier_unsent is not None:
                    continue
                result = session.execute(update(NotificationDeliveryRow).where(
                    NotificationDeliveryRow.id == row.id,
                    NotificationDeliveryRow.version == row.version,
                    or_(NotificationDeliveryRow.status == "pending",
                        (NotificationDeliveryRow.status == "sending")
                        & (NotificationDeliveryRow.lease_until < now))).values(
                        status="sending", owner=owner, lease_until=until,
                        first_attempt_at=func.coalesce(
                            NotificationDeliveryRow.first_attempt_at, now),
                        version=row.version + 1).execution_options(
                            synchronize_session=False))
                if result.rowcount == 1:
                    claimed.append(Delivery(
                        row.id, row.outbox_id, row.part_index, row.total_parts,
                        row.body, "sending", row.attempt_count, row.last_error_kind,
                        row.version + 1, outbox.priority,
                        _from_db_time(outbox.occurred_at),
                        _from_db_time(outbox.created_at)))
        return claimed

    def release_owner_deliveries(self, owner: str) -> int:
        """Return this sender's live leases and fence any late completion."""
        owner = safe_identifier(owner, "owner")
        with self._sessions.begin() as session:
            result = session.execute(update(NotificationDeliveryRow).where(
                NotificationDeliveryRow.status == "sending",
                NotificationDeliveryRow.owner == owner).values(
                status="pending", owner=None, lease_until=None,
                version=NotificationDeliveryRow.version + 1))
            return result.rowcount

    def finish_delivery(self, delivery_id: int, owner: str, version: int, *,
                        telegram_message_id: int, sent_at: datetime | None = None) -> bool:
        delivery_id = exact_nonnegative_int(delivery_id, "delivery_id")
        version = exact_nonnegative_int(version, "version")
        owner = safe_identifier(owner, "owner")
        now = _aware(sent_at) if sent_at else _aware(self._now())
        with self._sessions.begin() as session:
            delivery = session.get(NotificationDeliveryRow, delivery_id)
            if delivery is None:
                return False
            outbox = session.scalar(select(NotificationOutboxRow).where(
                NotificationOutboxRow.id == delivery.outbox_id).with_for_update())
            if outbox is None or outbox.status != "pending":
                return False
            result = session.execute(update(NotificationDeliveryRow).where(
                NotificationDeliveryRow.id == delivery_id,
                NotificationDeliveryRow.status == "sending",
                NotificationDeliveryRow.owner == owner,
                NotificationDeliveryRow.version == version).values(
                    status="sent", sent_at=now, telegram_message_id=telegram_message_id,
                    owner=None, lease_until=None, version=version + 1))
            if result.rowcount != 1:
                return False
            if outbox.sensitive:
                session.execute(update(NotificationDeliveryRow).where(
                    NotificationDeliveryRow.id == delivery_id).values(body=None))
                outbox.payload = None
            self._finish_outbox_if_complete(session, delivery.outbox_id, now)
            return True

    def _finish_outbox_if_complete(self, session, outbox_id: int, now: datetime) -> None:
        remaining = session.scalar(select(func.count()).select_from(
            NotificationDeliveryRow).where(
                NotificationDeliveryRow.outbox_id == outbox_id,
                NotificationDeliveryRow.status != "sent")) or 0
        if remaining == 0:
            session.execute(update(NotificationOutboxRow).where(
                NotificationOutboxRow.id == outbox_id).values(
                status="sent", sent_at=now, payload=None))
            session.execute(update(NotificationDeliveryRow).where(
                NotificationDeliveryRow.outbox_id == outbox_id).values(body=None))

    def retry_delivery(self, delivery_id: int, owner: str, version: int,
                   error_kind: str,
                   http_status: int | None, next_attempt_at: datetime | None = None) -> bool:
        now = _aware(self._now())
        delivery_id = exact_nonnegative_int(delivery_id, "delivery_id")
        version = exact_nonnegative_int(version, "version")
        owner = safe_identifier(owner, "owner")
        next_at = _aware(next_attempt_at) if next_attempt_at else now
        with self._sessions.begin() as session:
            delivery = session.get(NotificationDeliveryRow, delivery_id)
            if delivery is None:
                return False
            outbox = session.scalar(select(NotificationOutboxRow).where(
                NotificationOutboxRow.id == delivery.outbox_id).with_for_update())
            if outbox is None or outbox.status != "pending":
                return False
            result = session.execute(update(NotificationDeliveryRow).where(
                NotificationDeliveryRow.id == delivery_id,
                NotificationDeliveryRow.status == "sending",
                NotificationDeliveryRow.owner == owner,
                NotificationDeliveryRow.version == version).values(
                status="pending", owner=None, lease_until=None,
                attempt_count=NotificationDeliveryRow.attempt_count + 1,
                next_attempt_at=next_at, last_error_kind=error_kind,
                last_http_status=http_status, version=version + 1))
            if result.rowcount != 1:
                return False
            outbox.next_attempt_at = next_at
            outbox.last_error_kind = error_kind
            return True

    def dead_letter_delivery(self, delivery_id: int, owner: str, version: int,
                             error_kind: str, http_status: int | None) -> bool:
        """Fence one failed part and atomically terminalize its parent outbox."""
        delivery_id = exact_nonnegative_int(delivery_id, "delivery_id")
        version = exact_nonnegative_int(version, "version")
        owner = safe_identifier(owner, "owner")
        with self._sessions.begin() as session:
            delivery = session.get(NotificationDeliveryRow, delivery_id)
            if delivery is None:
                return False
            outbox = session.scalar(select(NotificationOutboxRow).where(
                NotificationOutboxRow.id == delivery.outbox_id).with_for_update())
            if outbox is None or outbox.status != "pending":
                return False
            result = session.execute(update(NotificationDeliveryRow).where(
                NotificationDeliveryRow.id == delivery_id,
                NotificationDeliveryRow.status == "sending",
                NotificationDeliveryRow.owner == owner,
                NotificationDeliveryRow.version == version).values(
                    status="dead_letter", owner=None, lease_until=None,
                    last_error_kind=error_kind, last_http_status=http_status,
                    version=version + 1))
            if result.rowcount != 1:
                return False
            session.execute(update(NotificationDeliveryRow).where(
                NotificationDeliveryRow.outbox_id == delivery.outbox_id,
                NotificationDeliveryRow.id != delivery_id,
                NotificationDeliveryRow.status != "sent").values(
                    status="dead_letter", owner=None, lease_until=None,
                    last_error_kind=error_kind,
                    last_http_status=http_status,
                    version=NotificationDeliveryRow.version + 1))
            outbox.status = "dead_letter"
            outbox.last_error_kind = error_kind
            return True

    def load_deliveries(self, outbox_id: int) -> list[Delivery]:
        with self._sessions() as session:
            rows = session.scalars(select(NotificationDeliveryRow).where(
                NotificationDeliveryRow.outbox_id == outbox_id).order_by(
                    NotificationDeliveryRow.part_index)).all()
            outbox = session.get(NotificationOutboxRow, outbox_id)
            if outbox is None:
                return []
            return [Delivery(
                r.id, r.outbox_id, r.part_index, r.total_parts, r.body, r.status,
                r.attempt_count, r.last_error_kind, r.version, outbox.priority,
                _from_db_time(outbox.occurred_at),
                _from_db_time(outbox.created_at)) for r in rows]

    def purge_expired_sensitive(self, now: datetime, limit: int = 1000) -> int:
        now = _aware(now)
        limit = positive_int(limit, "limit")
        purged = 0
        with self._sessions.begin() as session:
            ids = list(session.scalars(select(NotificationOutboxRow.id).where(
                NotificationOutboxRow.sensitive.is_(True),
                NotificationOutboxRow.purge_at <= now
            ).order_by(NotificationOutboxRow.id).limit(limit)))
            for outbox_id in ids:
                outbox = session.scalar(select(NotificationOutboxRow).where(
                    NotificationOutboxRow.id == outbox_id).with_for_update())
                live = session.scalar(select(func.count()).select_from(
                    NotificationDeliveryRow).where(
                        NotificationDeliveryRow.outbox_id == outbox_id,
                        NotificationDeliveryRow.status == "sending",
                        NotificationDeliveryRow.lease_until > now)) or 0
                if live:
                    continue
                body_count = session.scalar(select(func.count()).select_from(
                    NotificationDeliveryRow).where(
                        NotificationDeliveryRow.outbox_id == outbox_id,
                        NotificationDeliveryRow.body.is_not(None))) or 0
                if outbox.payload is None and body_count == 0:
                    continue
                session.execute(update(NotificationDeliveryRow).where(
                    NotificationDeliveryRow.outbox_id == outbox_id,
                    NotificationDeliveryRow.body.is_not(None)).values(
                    body=None,
                    status=case(
                        (NotificationDeliveryRow.status == "sent", "sent"),
                        else_="dead_letter"),
                    owner=None, lease_until=None,
                    version=NotificationDeliveryRow.version + 1))
                session.execute(update(NotificationOutboxRow).where(
                    NotificationOutboxRow.id == outbox_id).values(
                    payload=None, status="dead_letter",
                    last_error_kind="sensitive_payload_expired"))
                purged += 1
            return purged

    def outbox_payload(self, outbox_id: int) -> Any | None:
        with self._sessions() as session:
            value = session.scalar(select(NotificationOutboxRow.payload).where(
                NotificationOutboxRow.id == outbox_id))
            return json.loads(value) if value is not None else None

    def delivery_bodies(self, outbox_id: int) -> list[str | None]:
        with self._sessions() as session:
            return list(session.scalars(select(NotificationDeliveryRow.body).where(
                NotificationDeliveryRow.outbox_id == outbox_id).order_by(
                    NotificationDeliveryRow.part_index)))

    # 기존 Telegram 회귀 테스트와 호출부는 이름을 유지한다. 새 read-model
    # 계약은 보다 명확한 outbox_payload/delivery_bodies를 사용한다.
    def load_payload(self, outbox_id: int) -> Any | None:
        return self.outbox_payload(outbox_id)

    def load_delivery_bodies(self, outbox_id: int) -> list[str | None]:
        return self.delivery_bodies(outbox_id)

    def purge_retention(self, before: datetime, limit: int = 1000) -> int:
        """Delete only terminal metadata; unknown/unresolved and audit evidence remain."""
        before = _aware(before)
        with self._sessions.begin() as session:
            ids = list(session.scalars(select(NotificationOutboxRow.id).where(
                NotificationOutboxRow.status == "sent",
                NotificationOutboxRow.sent_at < before).order_by(
                    NotificationOutboxRow.id).limit(limit)))
            if not ids:
                return 0
            session.query(NotificationDeliveryRow).filter(
                NotificationDeliveryRow.outbox_id.in_(ids)).delete(
                    synchronize_session=False)
            session.query(NotificationOutboxRow).filter(
                NotificationOutboxRow.id.in_(ids)).delete(synchronize_session=False)
            return len(ids)

    def maintenance_cleanup(self, now: datetime, batch_size: int = 1000) -> int:
        """민감 본문 scrub과 1년 지난 sent 메타데이터 삭제의 총 상한을 지킨다."""
        now = _aware(now)
        batch_size = positive_int(batch_size, "batch_size")
        scrubbed = self.purge_expired_sensitive(now, limit=batch_size)
        remaining = batch_size - scrubbed
        if remaining <= 0:
            return scrubbed
        return scrubbed + self.purge_retention(
            now - timedelta(days=365), limit=remaining)


def _from_db_time(value: datetime) -> datetime:
    """Restore SQLite's lost timezone marker; this table only stores UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _latest_finished_or_started(rows) -> datetime | None:
    stamps = []
    for row in rows:
        if row is None:
            continue
        value = row.finished_at or row.started_at
        if value is not None:
            stamps.append(as_aware_utc(value))
    return max(stamps, default=None)
