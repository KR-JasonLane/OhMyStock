"""스코어링 오케스트레이션. CollectionService와 동일한 실행 패턴 —
원자적 start(), 태스크 강참조, 예외 경계(실패 시 run을 failed로 마감).
브로커 호출 없음: ScoringStore가 주는 데이터만 소비한다."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from app.core.background_service import BackgroundRunService
from app.core.market_calendar import scoring_reference_date
from app.domain.scoring.config import ScoringConfig
from app.domain.scoring.engine import run_scoring
from app.domain.scoring.strategies import Strategy, default_strategies
from app.domain.scoring.universe import passes_universe

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScoringProgress:
    # run_id는 create_run 이전의 시작 placeholder에서만 None (재실행 시 tear
    # 방지 — 아키텍트 패널 P5-T1). 그 외엔 항상 실제 정수 run_id.
    run_id: int | None
    status: str  # running | succeeded | failed
    stage: str   # starting | loading | gate | computing | saving | finished
    done: int
    total: int
    failure_reason: str | None = None


class ScoringService(BackgroundRunService):
    def __init__(self, store, config: ScoringConfig | None = None,
                 strategies: tuple[Strategy, ...] | None = None,
                 reference_provider: Callable[[], date] | None = None,
                 conflict_check: Callable[[], bool] | None = None) -> None:
        """reference_provider: 런 시작 시 1회 호출해 고정한다 (CollectionService와
        동일 계약) — 러닝 중 자정 등 날짜 경계를 통과해도 한 런 안에서는 신선도
        판정이 일관되도록 하기 위함.

        conflict_check: 반대편 서비스(CollectionService)가 실행 중인지 묻는
        콜러블. 상호 배제는 도메인 계약이다 — Phase 6 스케줄러가 HTTP를
        우회해 start()를 직접 호출해도 반쪽 데이터 읽기가 차단된다(API의
        409 응답은 사용자 메시지용 1차 관문일 뿐, 여기가 실제 방어선)."""
        super().__init__(task_label="scoring", conflict_check=conflict_check,
                          logger=logger)
        self._store = store
        self._config = config or ScoringConfig()
        self._strategies = strategies or default_strategies()
        self._reference_provider = reference_provider or scoring_reference_date
        self._progress: ScoringProgress | None = None

    def progress(self) -> ScoringProgress | None:
        return self._progress

    def latest_results(self) -> dict | None:
        """최근 succeeded 실행 결과 위임 — API가 store를 직접 알지 않도록
        분리한다. Phase 4도 이 진입점을 통해 스코어링 결과를 소비한다."""
        return self._store.latest_results()

    async def _run(self) -> None:
        """`_running` 복원은 베이스 `_execute()`의 finally가 구조적으로
        보장한다 — create_run 실패를 포함해 이 메서드 어디서 예외가 나든
        별도 처리가 필요 없다."""
        cfg = self._config
        reference = self._reference_provider()
        # create_run await 이전에 progress를 running으로 세팅 — 재실행 시 이전 런
        # 최종 progress가 새 실행 타임스탬프와 뒤섞이는 tear 방지(아키텍트 P5-T1).
        self._set(None, "running", "starting", 0, 0)
        run_id = await asyncio.to_thread(
            self._store.create_run, reference, cfg.to_json())
        universe_count = stale_excluded = 0
        try:
            self._set(run_id, "running", "loading", 0, 0)
            instruments = await asyncio.to_thread(
                self._store.active_common_instruments)
            universe = [sym for sym, audit, state in instruments
                        if passes_universe(audit, state)]
            universe_count = len(universe)
            if universe_count == 0:
                await self._fail(run_id, 0, 0,
                                 "empty universe - run collection first "
                                 "(instruments need audit_info/state fields)")
                return

            self._set(run_id, "running", "gate", 0, universe_count)
            latest = await asyncio.to_thread(self._store.latest_dates, universe)
            fresh = [s for s in universe
                     if latest.get(s) is not None and latest[s] >= reference]
            stale_excluded = universe_count - len(fresh)
            stale_ratio = stale_excluded / universe_count
            if stale_ratio > cfg.stale_exclusion_limit:
                await self._fail(
                    run_id, universe_count, stale_excluded,
                    f"stale data - run collection first "
                    f"({stale_ratio:.1%} > {cfg.stale_exclusion_limit:.1%}, "
                    f"reference={reference.isoformat()})")
                return

            self._set(run_id, "running", "computing", 0, len(fresh))
            members, names = await asyncio.to_thread(
                self._store.industry_memberships)
            fresh_set = set(fresh)
            members = {code: [s for s in ms if s in fresh_set]
                       for code, ms in members.items()}
            symbols = sorted({s for ms in members.values() for s in ms})
            candles = await asyncio.to_thread(self._store.load_candles, symbols)
            result = await asyncio.to_thread(
                run_scoring, members, names, candles, cfg, self._strategies)

            self._set(run_id, "running", "saving", len(fresh), len(fresh))
            await asyncio.to_thread(self._store.save_results, run_id, result)
            await asyncio.to_thread(self._store.finish_run, run_id, "succeeded",
                                    universe_count, stale_excluded, None)
            self._set(run_id, "succeeded", "finished",
                      len(fresh), len(fresh))
            logger.info(
                "scoring run %d: %d candidates from %d sectors "
                "(universe=%d, stale=%d, short_history=%d)",
                run_id, len(result.candidates),
                sum(1 for s in result.sectors if s.selected), universe_count,
                stale_excluded, result.excluded_short_history)
        except asyncio.CancelledError:
            await self._fail(run_id, universe_count, stale_excluded, "cancelled")
            raise
        except Exception as exc:
            logger.exception("scoring run %s failed unexpectedly", run_id)
            await self._fail(run_id, universe_count, stale_excluded,
                             f"unexpected: {type(exc).__name__}")
            raise

    async def _fail(self, run_id: int, universe_count: int,
                    stale_excluded: int, reason: str) -> None:
        logger.warning("scoring run %d rejected: %s", run_id, reason)
        await asyncio.to_thread(self._store.finish_run, run_id, "failed",
                                universe_count, stale_excluded, reason)
        self._set(run_id, "failed", "finished", 0, universe_count, reason)

    def _set(self, run_id: int | None, status: str, stage: str, done: int,
             total: int, failure_reason: str | None = None) -> None:
        self._progress = ScoringProgress(run_id, status, stage, done, total,
                                         failure_reason)
