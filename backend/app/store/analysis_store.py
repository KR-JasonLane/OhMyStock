"""AI 분석 결과 영속화·조회. 동기 SQLAlchemy — 서비스가 asyncio.to_thread로 호출.
결과는 run 단위 insert-only (`ScoringStore`와 동일 패턴 — 스펙 §6)."""

import json
from collections.abc import Callable
from datetime import date, datetime, timezone

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import sessionmaker

from app.domain.analysis.graph import AnalysisResult
from app.domain.analysis.ports import (CandidateInput, Headline,
                                       MarketSnapshot, StrategyDetailInput)
from app.store.models import (AnalysisNewsRow, AnalysisRunRow,
                              AnalysisVerdictRow, InstrumentRow, ScoreDetailRow,
                              ScoreRow, ScoreRunRow, ScoreSectorRow, SectorRow)


class AnalysisStore:
    def __init__(self, engine: Engine,
                 now: Callable[[], datetime] | None = None) -> None:
        self._sessions = sessionmaker(bind=engine)
        self._now = now or (lambda: datetime.now(timezone.utc))

    # ---------- 스코어링 결과 조회 (분석 입력) ----------

    def latest_succeeded_score_run(self) -> tuple[int, date] | None:
        """가장 최근 succeeded 스코어링 런의 (score_run_id, reference_date).
        없으면 None — 분석 파이프라인이 실행할 스코어링 결과가 아직 없다는 뜻."""
        with self._sessions() as session:
            row = session.execute(
                select(ScoreRunRow.id, ScoreRunRow.reference_date)
                .where(ScoreRunRow.status == "succeeded")
                .order_by(ScoreRunRow.id.desc()).limit(1)).first()
            return tuple(row) if row is not None else None

    def load_candidates(self, score_run_id: int) -> list[CandidateInput]:
        """scores × score_details × instruments(name) × sectors(name via
        scores.sector_code) 조인.

        불변식: 반환 순서는 scores.rank 오름차순이다 — T5 파이프라인의
        trader 노드가 이 순서로 후보를 순회하며, 최종 선정 순위는
        `synthesize`가 confidence*total_score 기준으로 별도 재정렬하므로
        입력 순서 자체가 결과를 바꾸지는 않지만, 재현성(동일 입력 → 동일
        호출 순서)을 위해 계약으로 고정한다."""
        with self._sessions() as session:
            score_rows = session.execute(
                select(ScoreRow.symbol, ScoreRow.total_score, ScoreRow.sector_code,
                       ScoreRow.sector_score, ScoreRow.strategy_score_norm,
                       InstrumentRow.name, SectorRow.name)
                .join(InstrumentRow, InstrumentRow.symbol == ScoreRow.symbol)
                .join(SectorRow, SectorRow.code == ScoreRow.sector_code)
                .where(ScoreRow.run_id == score_run_id)
                .order_by(ScoreRow.rank)).all()

            detail_rows = session.execute(
                select(ScoreDetailRow.symbol, ScoreDetailRow.strategy,
                       ScoreDetailRow.signal, ScoreDetailRow.avg_return,
                       ScoreDetailRow.win_rate, ScoreDetailRow.occurrences)
                .where(ScoreDetailRow.run_id == score_run_id)
                .order_by(ScoreDetailRow.symbol, ScoreDetailRow.strategy)).all()

        details_by_symbol: dict[str, list[StrategyDetailInput]] = {}
        for symbol, strategy, signal, avg_return, win_rate, occurrences in detail_rows:
            details_by_symbol.setdefault(symbol, []).append(StrategyDetailInput(
                strategy=strategy, signal=signal, avg_return=avg_return,
                win_rate=win_rate, occurrences=occurrences))

        return [
            CandidateInput(
                symbol=symbol, name=name, sector_name=sector_name,
                total_score=total_score, sector_score=sector_score,
                strategy_score_norm=strategy_score_norm,
                details=tuple(details_by_symbol.get(symbol, ())))
            for symbol, total_score, sector_code, sector_score,
                strategy_score_norm, name, sector_name in score_rows]

    def market_snapshot(self, score_run_id: int) -> MarketSnapshot:
        """score_sectors 전 행(industry만 저장돼 있음 — ScoringService가
        aggregate 업종을 미리 걸러낸다)으로 "업종명 r5 r20 r60" 표를 만든다.

        줄 정렬: 업종명 오름차순(동률은 sector_code로 tie-break) — 이코노미스트
        프롬프트를 실행마다 결정론적인 순서로 보여주기 위함(내용상 의미는
        없다). breadth: R20 > 0인 업종 비율. 행이 0개면 breadth 0.0, 표는
        빈 문자열."""
        with self._sessions() as session:
            rows = session.execute(
                select(SectorRow.name, ScoreSectorRow.sector_code,
                       ScoreSectorRow.r5, ScoreSectorRow.r20, ScoreSectorRow.r60)
                .join(SectorRow, SectorRow.code == ScoreSectorRow.sector_code)
                .where(ScoreSectorRow.run_id == score_run_id)).all()

        if not rows:
            return MarketSnapshot(sector_table="", breadth=0.0)

        ordered = sorted(rows, key=lambda r: (r[0], r[1]))
        lines = [f"{name} {r5:.4f} {r20:.4f} {r60:.4f}"
                 for name, _code, r5, r20, r60 in ordered]
        breadth = sum(1 for *_rest, r20, _r60 in rows if r20 > 0) / len(rows)
        return MarketSnapshot(sector_table="\n".join(lines), breadth=breadth)

    # ---------- run 라이프사이클 ----------

    def create_run(self, score_run_id: int, model: str, prompt_hash: str,
                   config_json: str) -> int:
        with self._sessions.begin() as session:
            run = AnalysisRunRow(
                started_at=self._now(), status="running",
                score_run_id=score_run_id, model=model, prompt_hash=prompt_hash,
                config=config_json)
            session.add(run)
            session.flush()
            return run.id

    def finish_run(self, run_id: int, status: str, regime: str | None = None,
                   market_summary: str | None = None,
                   warnings: str | None = None,
                   failure_reason: str | None = None) -> None:
        with self._sessions.begin() as session:
            run = session.get(AnalysisRunRow, run_id)
            if run is None:
                return
            run.finished_at = self._now()
            run.status = status
            run.regime = regime
            run.market_summary = market_summary
            run.warnings = warnings
            run.failure_reason = failure_reason

    def save_results(self, run_id: int, result: AnalysisResult,
                     news: dict[str, list[Headline]]) -> None:
        """verdicts/picks/news 3테이블 insert. news 키는 "market" 또는
        종목코드(스코프)."""
        with self._sessions.begin() as session:
            pick_rank_by_symbol = {p.symbol: p.rank for p in result.picks}
            for symbol, verdict in result.verdicts.items():
                session.add(AnalysisVerdictRow(
                    run_id=run_id, symbol=symbol, verdict=verdict.verdict,
                    confidence=verdict.confidence,
                    reasons=json.dumps(list(verdict.reasons), ensure_ascii=False),
                    risk_flags=json.dumps(list(verdict.risk_flags), ensure_ascii=False),
                    picked=symbol in pick_rank_by_symbol,
                    pick_rank=pick_rank_by_symbol.get(symbol)))
            for scope, headlines in news.items():
                for h in headlines:
                    session.add(AnalysisNewsRow(
                        run_id=run_id, scope=scope, url=h.url, title=h.title,
                        published_at=h.published_at))

    def latest_results(self) -> dict | None:
        """최근 succeeded 실행의 결과 (API 응답 본문). 뉴스 스냅샷 자체는
        (헤드라인 원문 다수라) 크고 API 응답에 부적합하므로 개수만 포함한다
        — 상세 복기가 필요하면 analysis_news 테이블을 직접 조회한다."""
        with self._sessions() as session:
            run = session.scalars(
                select(AnalysisRunRow).where(AnalysisRunRow.status == "succeeded")
                .order_by(AnalysisRunRow.id.desc()).limit(1)).first()
            if run is None:
                return None
            verdicts = session.scalars(
                select(AnalysisVerdictRow).where(AnalysisVerdictRow.run_id == run.id)
                .order_by(AnalysisVerdictRow.symbol)).all()
            picks = sorted(
                (v for v in verdicts if v.picked and v.pick_rank is not None),
                key=lambda v: v.pick_rank)
            news_count = session.scalar(
                select(func.count()).select_from(AnalysisNewsRow)
                .where(AnalysisNewsRow.run_id == run.id))
            return {
                "run_id": run.id,
                "score_run_id": run.score_run_id,
                "model": run.model,
                "prompt_hash": run.prompt_hash,
                "started_at": run.started_at.isoformat(),
                "finished_at": (run.finished_at.isoformat()
                                if run.finished_at else None),
                "status": run.status,
                "regime": run.regime,
                "market_summary": run.market_summary,
                "warnings": run.warnings,
                "failure_reason": run.failure_reason,
                "picks": [{"symbol": v.symbol, "rank": v.pick_rank} for v in picks],
                "verdicts": [
                    {"symbol": v.symbol, "verdict": v.verdict,
                     "confidence": v.confidence,
                     "reasons": json.loads(v.reasons),
                     "risk_flags": json.loads(v.risk_flags),
                     "picked": v.picked, "pick_rank": v.pick_rank}
                    for v in verdicts],
                "news_count": news_count,
            }
