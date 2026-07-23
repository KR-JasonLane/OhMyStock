"""스코어링 영속화·조회. 동기 SQLAlchemy — 서비스가 asyncio.to_thread로 호출.
결과는 run 단위 insert-only (upsert 불필요 — 스펙 §5)."""

import json
from collections.abc import Callable
from datetime import date, datetime, timezone

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import sessionmaker

from app.domain.broker import Candle
from app.domain.scoring.engine import ScoringResult
from app.store.kst_time import as_aware_utc
from app.store.models import (CandleRow, InstrumentRow, ScoreDetailRow,
                              ScoreRow, ScoreRunRow, ScoreSectorRow,
                              SectorMembershipRow, SectorRow)


class ScoringStore:
    def __init__(self, engine: Engine,
                 now: Callable[[], datetime] | None = None) -> None:
        self._sessions = sessionmaker(bind=engine)
        self._now = now or (lambda: datetime.now(timezone.utc))

    # ---------- run 라이프사이클 ----------

    def create_run(self, reference_date: date, config_json: str) -> int:
        with self._sessions.begin() as session:
            run = ScoreRunRow(started_at=self._now(), status="running",
                              reference_date=reference_date, config=config_json)
            session.add(run)
            session.flush()
            return run.id

    def finish_run(self, run_id: int, status: str, universe_count: int = 0,
                   stale_excluded: int = 0,
                   failure_reason: str | None = None) -> None:
        with self._sessions.begin() as session:
            run = session.get(ScoreRunRow, run_id)
            if run is None:
                return
            run.finished_at = self._now()
            run.status = status
            run.universe_count = universe_count
            run.stale_excluded = stale_excluded
            run.failure_reason = failure_reason

    def save_results(self, run_id: int, result: ScoringResult) -> None:
        with self._sessions.begin() as session:
            for s in result.sectors:
                session.add(ScoreSectorRow(
                    run_id=run_id, sector_code=s.code, r20=s.r20, r60=s.r60,
                    r5=s.r5, score=s.score, rank=s.rank, selected=s.selected))
            for c in result.candidates:
                session.add(ScoreRow(
                    run_id=run_id, symbol=c.symbol, rank=c.rank,
                    total_score=c.total_score, sector_code=c.sector_code,
                    sector_score=c.sector_score,
                    strategy_score=c.strategy_score,
                    strategy_score_norm=c.strategy_score_norm))
                for d in c.details:
                    session.add(ScoreDetailRow(
                        run_id=run_id, symbol=c.symbol, strategy=d.strategy,
                        signal=d.signal, avg_return=d.avg_return,
                        win_rate=d.win_rate, occurrences=d.occurrences,
                        score=d.score))

    def latest_results(self) -> dict | None:
        """최근 succeeded 실행의 전체 결과 (API /score/latest 응답 본문)."""
        with self._sessions() as session:
            run = session.scalars(
                select(ScoreRunRow).where(ScoreRunRow.status == "succeeded")
                .order_by(ScoreRunRow.id.desc()).limit(1)).first()
            if run is None:
                return None
            sectors = session.scalars(
                select(ScoreSectorRow).where(ScoreSectorRow.run_id == run.id)
                .order_by(ScoreSectorRow.rank)).all()
            scores = session.scalars(
                select(ScoreRow).where(ScoreRow.run_id == run.id)
                .order_by(ScoreRow.rank)).all()
            details = session.scalars(
                select(ScoreDetailRow).where(ScoreDetailRow.run_id == run.id)).all()
            by_symbol: dict[str, list[dict]] = {}
            for d in details:
                by_symbol.setdefault(d.symbol, []).append(
                    {"strategy": d.strategy, "signal": d.signal,
                     "avg_return": d.avg_return, "win_rate": d.win_rate,
                     "occurrences": d.occurrences, "score": d.score})
            return {
                "run_id": run.id,
                "reference_date": run.reference_date.isoformat(),
                "finished_at": (run.finished_at.isoformat()
                                if run.finished_at else None),
                "config": json.loads(run.config),
                "sectors": [
                    {"code": s.sector_code, "r20": s.r20, "r60": s.r60,
                     "r5": s.r5, "score": s.score, "rank": s.rank,
                     "selected": s.selected} for s in sectors],
                "candidates": [
                    {"symbol": s.symbol, "rank": s.rank,
                     "total_score": s.total_score, "sector_code": s.sector_code,
                     "sector_score": s.sector_score,
                     "strategy_score": s.strategy_score,
                     "strategy_score_norm": s.strategy_score_norm,
                     "details": by_symbol.get(s.symbol, [])} for s in scores],
            }

    # ---------- 스코어링 입력 조회 ----------

    def active_common_instruments(self) -> list[tuple[str, str, str]]:
        """(symbol, audit_info, state) — kospi/kosdaq 활성 종목만 (etf 제외).
        audit_info/state 값에 따른 정상/비정상 필터링(스펙 §4-2: '정상' 아님,
        '거래정지'·'관리종목' 포함 등)은 이 메서드의 책임이 아니다 — 호출자
        (ScoringService §4-2)가 반환된 raw 값을 보고 판단한다."""
        with self._sessions() as session:
            rows = session.execute(
                select(InstrumentRow.symbol, InstrumentRow.audit_info,
                       InstrumentRow.state)
                .where(InstrumentRow.market.in_(("kospi", "kosdaq")),
                       InstrumentRow.is_active.is_(True))).all()
            return [tuple(r) for r in rows]

    def industry_memberships(self) -> tuple[dict[str, list[str]], dict[str, str]]:
        """group_type='industry' 업종의 소속과 이름."""
        with self._sessions() as session:
            names = dict(session.execute(
                select(SectorRow.code, SectorRow.name)
                .where(SectorRow.group_type == "industry")).all())
            members: dict[str, list[str]] = {}
            rows = session.execute(
                select(SectorMembershipRow.sector_code,
                       SectorMembershipRow.symbol)
                .where(SectorMembershipRow.sector_code.in_(names))).all()
            for code, symbol in rows:
                members.setdefault(code, []).append(symbol)
            return members, names

    def latest_dates(self, symbols: list[str]) -> dict[str, date]:
        with self._sessions() as session:
            rows = session.execute(
                select(CandleRow.symbol, func.max(CandleRow.date))
                .where(CandleRow.symbol.in_(symbols))
                .group_by(CandleRow.symbol)).all()
            return {symbol: latest for symbol, latest in rows}

    def load_candles(self, symbols: list[str]) -> dict[str, list[Candle]]:
        """과거→최신 정렬 보장. 튜플 select — 2,500종목×600봉 ORM 오버헤드 회피."""
        result: dict[str, list[Candle]] = {}
        with self._sessions() as session:
            rows = session.execute(
                select(CandleRow.symbol, CandleRow.date, CandleRow.open,
                       CandleRow.high, CandleRow.low, CandleRow.close,
                       CandleRow.volume)
                .where(CandleRow.symbol.in_(symbols))
                .order_by(CandleRow.symbol, CandleRow.date)).all()
        for symbol, d, o, h, lo, c, v in rows:
            result.setdefault(symbol, []).append(
                Candle(symbol=symbol, date=d, open=o, high=h, low=lo,
                       close=c, volume=v))
        return result

    # ---------- P6 스케줄러 판정 헬퍼 (read-only, 스펙 §6) ----------

    def has_completed_run(self, reference_date: date) -> bool:
        """reference_date=R인 succeeded run 존재 — 스케줄러 "몫 완료" 판정.
        스코어링만 시작 시각이 아니라 reference_date 컬럼으로 판정한다
        (자정을 넘는 창 — 스펙 §4-b — 에서도 몫 귀속이 명확)."""
        with self._sessions() as session:
            return session.scalar(
                select(ScoreRunRow.id)
                .where(ScoreRunRow.status == "succeeded",
                       ScoreRunRow.reference_date == reference_date)
                .limit(1)) is not None

    def last_failed_finished_at(self, reference_date: date) -> datetime | None:
        """reference_date=R인 failed run의 마지막 종료 시각 — 백오프 기준."""
        with self._sessions() as session:
            stamps = session.scalars(
                select(ScoreRunRow.finished_at)
                .where(ScoreRunRow.status == "failed",
                       ScoreRunRow.reference_date == reference_date,
                       ScoreRunRow.finished_at.is_not(None))).all()
        return max((as_aware_utc(s) for s in stamps), default=None)
