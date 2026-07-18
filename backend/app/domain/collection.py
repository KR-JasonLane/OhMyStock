"""수집 오케스트레이션. BrokerPort와 Store 메서드 계약만 안다 (키움/SQL 무지).
Store 호출은 동기이므로 asyncio.to_thread로 이벤트 루프를 막지 않는다."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from app.core.market_calendar import previous_weekday
from app.domain.broker import BrokerPort
from app.domain.errors import AuthError, BrokerError, RateLimitError
from app.domain.sector_classification import UNCLASSIFIED, classify_sector

logger = logging.getLogger(__name__)


def _log_task_exception(task: asyncio.Task) -> None:
    """collection_task의 done 콜백 — 취소가 아니면서 예외로 끝났다면 로깅한다.

    fire-and-forget 태스크(asyncio.create_task)는 예외를 조회하지 않으면
    조용히 삼켜지고 "Task exception was never retrieved" 경고만 남는다.
    run()/_run() 내부에서 이미 실패 상태를 store에 기록하지만, 이 콜백은
    태스크 자체의 예외를 놓치지 않기 위한 마지막 안전망이다.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("collection task failed: %s", exc)


# 상장폐지 반영(deactivate_missing) 실행 전제 — 이 전체 시장 집합과 markets가
# 정확히 일치할 때만 반영한다 (아래 __init__ docstring 참고).
_ALL_MARKETS = frozenset({"kospi", "kosdaq", "etf"})


@dataclass(frozen=True)
class CollectionProgress:
    run_id: int
    status: str  # running | done | failed
    stage: str   # instruments | sectors | candles | finished
    done: int
    total: int
    failed: int
    warning: str | None = None


class CollectionService:
    def __init__(self, broker: BrokerPort, store,
                 markets: tuple[str, ...] = ("kospi", "kosdaq", "etf"),
                 candle_count: int = 600,
                 max_consecutive_failures: int = 20,
                 reference_provider: Callable[[], date] | None = None) -> None:
        """markets: 수집 대상 시장 목록.

        reference_provider: candles 단계에서 스킵 판단에 쓸 기준일을 반환하는
        콜러블 (기본 `previous_weekday` — 휴장일 캘린더 없는 근사, 공휴일에는
        늦은 날짜를 반환할 수 있어 스킵이 풀리고 재수집되지만 멱등이라 안전).
        런 시작 시 1회만 호출해 고정한다. 과거에는 "이번 런에서 처음 성공한
        종목의 최신 봉 일자"를 러닝 앵커로 삼았는데, 그 첫 종목이 장기
        거래정지 등으로 낡은 봉만 반환하면 기준일 자체가 낡은 채 고정되어
        이후 전 종목이 영구 스킵되는 결함이 있었다 — 달력 기준은 특정 종목의
        조회 결과와 무관하므로 이 결함이 없다. 테스트에서는 결정론적 고정
        날짜를 주입한다.

        계약: 상장폐지 반영(`store.deactivate_missing`)은 `markets`가 전체 시장
        집합(`{"kospi", "kosdaq", "etf"}`)과 정확히 일치하고, 모든 시장에서 1건
        이상 수집됐을 때만 실행된다. 부분 시장 호출이나 빈 응답에서는 실행하지
        않는다 — 그렇지 않으면 조회되지 않은 시장의 종목 전체가 "이번 런에 없음"
        으로 오판되어 비활성화되는 사고로 이어진다.
        """
        self._broker = broker
        self._store = store
        self._markets = markets
        self._candle_count = candle_count
        self._max_consec = max_consecutive_failures
        self._reference_provider = reference_provider or previous_weekday
        self._running = False
        self._progress: CollectionProgress | None = None
        self._warning: str | None = None
        self._task: asyncio.Task | None = None

    def is_running(self) -> bool:
        return self._running

    def progress(self) -> CollectionProgress | None:
        return self._progress

    def current_task(self) -> asyncio.Task | None:
        return self._task

    def start(self, warning: str | None = None) -> asyncio.Task | None:
        """원자적 시작: 이미 실행 중이면 None. 태스크 강참조는 서비스가 보유한다.

        check(self._running)와 set(self._running = True) 사이에 await가 없어
        원자적이다 — API 레이어의 별도 락 없이도 동시 POST 요청 중 하나만
        시작을 얻는다(TOCTOU 없음). 태스크 자체를 여기서 보유하므로 호출자가
        GC로 태스크를 잃어버릴 걱정도 없다.
        """
        if self._running:
            return None
        self._running = True
        self._warning = warning
        self._task = asyncio.create_task(self._run())
        self._task.add_done_callback(_log_task_exception)
        return self._task

    async def run(self) -> None:
        """단독 호출용 진입점 (테스트/스크립트). API는 start()를 쓴다."""
        if self._running:
            raise RuntimeError("collection already running")
        self._running = True
        self._warning = None
        await self._run()

    async def _run(self) -> None:
        """전체 수집 파이프라인을 실행한다 (instruments → sectors → candles).

        정규장(09:00-15:30 KST) 종료 후 실행해야 한다 — 장중 실행 시 미확정
        당일 봉이 저장되고, `latest_candle_dates` 기반 달력 스킵 로직이 이
        미확정 봉을 "이미 최신"으로 오판해 고착시킬 수 있다. 실행 시각 강제는
        이 서비스의 책임이 아니라 Phase 6 스케줄러가 진다 (거래일 캘린더가
        필요하기 때문).
        """
        run_id = await asyncio.to_thread(self._store.create_run)
        succeeded = failed = total = 0
        notes: list[str] = []
        try:
            self._set(run_id, "running", "instruments", 0, 0, 0)
            seen: set[str] = set()
            per_market_counts: dict[str, int] = {}
            for market in self._markets:
                instruments = await self._broker.list_instruments(market)
                await asyncio.to_thread(self._store.upsert_instruments, instruments)
                per_market_counts[market] = len(instruments)
                seen.update(i.symbol for i in instruments)

            if (set(self._markets) == _ALL_MARKETS
                    and all(count > 0 for count in per_market_counts.values())):
                deactivated = await asyncio.to_thread(
                    self._store.deactivate_missing, seen)
                logger.info("deactivated %d symbols missing from this run", deactivated)
            else:
                logger.warning(
                    "skipping deactivation: partial/empty market data %s",
                    per_market_counts)

            self._set(run_id, "running", "sectors", 0, 0, 0)
            sectors = await self._broker.list_sectors()
            group_types = {s.code: classify_sector(s.code) for s in sectors}
            unclassified_codes = {s.code for s in sectors
                                  if group_types[s.code] == UNCLASSIFIED}
            for s in sectors:
                if group_types[s.code] == UNCLASSIFIED:
                    # 분류 맵(2026-07-18 실측 65개)에 없는 신설 코드 — 소비
                    # 제외되므로 동작은 안전하나 맵 갱신이 필요하다는 신호.
                    logger.warning("unclassified sector code %s (%s) - "
                                   "update sector_classification map",
                                   s.code, s.name)
            if unclassified_codes:
                notes.append(
                    f"unclassified sector codes: {sorted(unclassified_codes)[:10]} "
                    f"(count={len(unclassified_codes)}) - update sector_classification map")
            await asyncio.to_thread(self._store.upsert_sectors, sectors,
                                    group_types)
            memberships: dict[str, list[str]] = {}
            failed_sector_codes: list[str] = []
            for sector in sectors:
                try:
                    memberships[sector.code] = await self._broker.list_sector_members(
                        sector.code, sector.market)
                except (AuthError, RateLimitError):
                    raise  # 서버/인증 장애 — 업종 격리 대상이 아님, 전체 중단
                except BrokerError as exc:
                    failed_sector_codes.append(sector.code)
                    logger.warning("sector membership fetch failed for %s (%s): %s",
                                   sector.code, sector.name, exc)
            if failed_sector_codes:
                # 전체 교체(delete-and-insert) 의미론에서 부분 수집분으로 교체하면
                # 실패 업종의 기존 소속이 삭제되어 그 업종이 로테이션에서 소실된다
                # — 낡았지만 완전한 직전 스냅샷 보존이 매매 관점에서 안전. 멤버십은
                # 느리게 변하는 데이터라 하루 지연은 수용 가능.
                logger.warning(
                    "skipping sector membership replace: %d/%d sectors failed "
                    "(sample: %s) - keeping previous snapshot",
                    len(failed_sector_codes), len(sectors), failed_sector_codes[:5])
                notes.append(
                    f"sector memberships NOT replaced - {len(failed_sector_codes)} "
                    f"fetch failures (sample: {failed_sector_codes[:5]}), previous "
                    "snapshot kept")
            else:
                n = await asyncio.to_thread(
                    self._store.replace_sector_memberships, memberships)
                logger.info("stored %d sector membership rows across %d groups",
                            n, len(sectors))

            symbols = await asyncio.to_thread(self._store.list_symbols)
            total = len(symbols)
            latest_dates = await asyncio.to_thread(self._store.latest_candle_dates)
            reference = self._reference_provider()
            consecutive = 0
            for i, symbol in enumerate(symbols, start=1):
                latest = latest_dates.get(symbol)
                if latest is not None and latest >= reference:
                    succeeded += 1
                else:
                    try:
                        candles = await self._broker.get_daily_candles(
                            symbol, self._candle_count)
                    except (AuthError, RateLimitError):
                        raise  # 서버/인증 장애 — 종목 격리 대상이 아님
                    except BrokerError as exc:
                        failed += 1
                        consecutive += 1
                        logger.warning("collect failed for %s: %s", symbol, exc)
                        if consecutive > self._max_consec:
                            raise BrokerError(
                                f"aborted after {consecutive} consecutive failures"
                            ) from exc
                    else:
                        consecutive = 0
                        if candles:
                            await asyncio.to_thread(self._store.upsert_candles, candles)
                        succeeded += 1
                self._set(run_id, "running", "candles", i, total, failed)

            if notes:
                self._warning = "; ".join(filter(None, [self._warning, *notes]))
            await asyncio.to_thread(self._store.finish_run, run_id, "done",
                                    total, succeeded, failed,
                                    "; ".join(notes) if notes else None)
            self._set(run_id, "done", "finished", total, total, failed)
        except BrokerError as exc:
            await asyncio.to_thread(self._store.finish_run, run_id, "failed",
                                    total, succeeded, failed, str(exc))
            self._set(run_id, "failed", "finished", succeeded + failed, total, failed)
        except asyncio.CancelledError:
            await asyncio.to_thread(self._store.finish_run, run_id, "failed",
                                    total, succeeded, failed, "cancelled")
            self._set(run_id, "failed", "finished", succeeded + failed, total, failed)
            raise
        except Exception as exc:
            logger.exception("collection run %s failed unexpectedly", run_id)
            await asyncio.to_thread(self._store.finish_run, run_id, "failed",
                                    total, succeeded, failed,
                                    f"unexpected: {type(exc).__name__}")
            self._set(run_id, "failed", "finished", succeeded + failed, total, failed)
            raise
        finally:
            self._running = False

    def _set(self, run_id: int, status: str, stage: str,
             done: int, total: int, failed: int) -> None:
        self._progress = CollectionProgress(
            run_id, status, stage, done, total, failed, self._warning)
