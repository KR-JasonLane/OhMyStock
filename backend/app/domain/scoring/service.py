"""스코어링 오케스트레이션. CollectionService와 동일한 실행 패턴 —
원자적 start(), 태스크 강참조, 예외 경계(실패 시 run을 failed로 마감).
브로커 호출 없음: ScoringStore가 주는 데이터만 소비한다."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from app.core.market_calendar import previous_weekday
from app.domain.scoring.config import ScoringConfig
from app.domain.scoring.engine import run_scoring
from app.domain.scoring.strategies import Strategy, default_strategies

logger = logging.getLogger(__name__)


def _log_task_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("scoring task failed: %s", exc)


@dataclass(frozen=True)
class ScoringProgress:
    run_id: int
    status: str  # running | succeeded | failed
    stage: str   # loading | gate | computing | saving | finished
    done: int
    total: int
    failure_reason: str | None = None


def _passes_universe(audit_info: str, state: str) -> bool:
    """스펙 §4-2: auditInfo가 "정상"이고 state에 거래정지/관리종목 플래그가 없다."""
    return (audit_info == "정상"
            and "거래정지" not in state
            and "관리종목" not in state)


class ScoringService:
    def __init__(self, store, config: ScoringConfig | None = None,
                 strategies: tuple[Strategy, ...] | None = None,
                 reference_provider: Callable[[], date] | None = None) -> None:
        self._store = store
        self._config = config or ScoringConfig()
        self._strategies = strategies or default_strategies()
        self._reference_provider = reference_provider or previous_weekday
        self._running = False
        self._progress: ScoringProgress | None = None
        self._task: asyncio.Task | None = None

    def is_running(self) -> bool:
        return self._running

    def progress(self) -> ScoringProgress | None:
        return self._progress

    def current_task(self) -> asyncio.Task | None:
        return self._task

    def start(self) -> asyncio.Task | None:
        """원자적 시작 — check와 set 사이에 await 없음 (CollectionService와 동일)."""
        if self._running:
            return None
        self._running = True
        self._task = asyncio.create_task(self._run())
        self._task.add_done_callback(_log_task_exception)
        return self._task

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("scoring already running")
        self._running = True
        await self._run()

    async def _run(self) -> None:
        cfg = self._config
        reference = self._reference_provider()
        run_id = await asyncio.to_thread(
            self._store.create_run, reference, cfg.to_json())
        universe_count = stale_excluded = 0
        try:
            self._set(run_id, "running", "loading", 0, 0)
            instruments = await asyncio.to_thread(
                self._store.active_common_instruments)
            universe = [sym for sym, audit, state in instruments
                        if _passes_universe(audit, state)]
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
            await asyncio.to_thread(self._store.finish_run, run_id, "failed",
                                    universe_count, stale_excluded, "cancelled")
            self._set(run_id, "failed", "finished", 0, universe_count,
                      "cancelled")
            raise
        except Exception as exc:
            logger.exception("scoring run %s failed unexpectedly", run_id)
            await asyncio.to_thread(self._store.finish_run, run_id, "failed",
                                    universe_count, stale_excluded,
                                    f"unexpected: {type(exc).__name__}")
            self._set(run_id, "failed", "finished", 0, universe_count,
                      f"unexpected: {type(exc).__name__}")
            raise
        finally:
            self._running = False

    async def _fail(self, run_id: int, universe_count: int,
                    stale_excluded: int, reason: str) -> None:
        logger.warning("scoring run %d rejected: %s", run_id, reason)
        await asyncio.to_thread(self._store.finish_run, run_id, "failed",
                                universe_count, stale_excluded, reason)
        self._set(run_id, "failed", "finished", 0, universe_count, reason)

    def _set(self, run_id: int, status: str, stage: str, done: int,
             total: int, failure_reason: str | None = None) -> None:
        self._progress = ScoringProgress(run_id, status, stage, done, total,
                                         failure_reason)
