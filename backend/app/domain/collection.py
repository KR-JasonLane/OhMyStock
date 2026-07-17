"""수집 오케스트레이션. BrokerPort와 Store 메서드 계약만 안다 (키움/SQL 무지).
Store 호출은 동기이므로 asyncio.to_thread로 이벤트 루프를 막지 않는다."""

import asyncio
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from app.core.market_calendar import previous_weekday
from app.domain.broker import BrokerPort
from app.domain.errors import AuthError, BrokerError, RateLimitError

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


# 집계성 업종명 마커 — 실측 근거: 종합(001)이 시장 전체 2,477종목을 포함해 개별
# 업종과 성격이 다름 (docs/STATUS.md Phase 3 PRE-GATE 참고). 이름 문자열 매칭은
# 휴리스틱이며 코드값 기반 확정은 Phase 3 PRE-GATE에서 다룬다.
_AGGREGATE_SECTOR_NAME_MARKERS = ("종합", "대형주", "중형주", "소형주")

# 실측으로 확정된 집계 업종 코드 — 001(종합 KOSPI), 101(종합 KOSDAQ). 이름 마커보다
# 신뢰도가 높은 1차 필터.
_AGGREGATE_SECTOR_CODES = frozenset({"001", "101"})

# 상장폐지 반영(deactivate_missing) 실행 전제 — 이 전체 시장 집합과 markets가
# 정확히 일치할 때만 반영한다 (아래 __init__ docstring 참고).
_ALL_MARKETS = frozenset({"kospi", "kosdaq", "etf"})

# 매핑 캐너리 임계값 — 단일 업종코드가 매핑의 이 비율을 넘으면 집계 업종 누출
# 의심으로 경고만 남긴다 (중단하지 않음, 휴리스틱).
_CANARY_SHARE_THRESHOLD = 0.5


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
            await asyncio.to_thread(self._store.upsert_sectors, sectors)
            mapping: dict[str, str] = {}
            skipped_sectors = 0
            for sector in sectors:
                if (sector.code in _AGGREGATE_SECTOR_CODES
                        or any(m in sector.name for m in _AGGREGATE_SECTOR_NAME_MARKERS)):
                    skipped_sectors += 1
                    continue
                for symbol in await self._broker.list_sector_members(
                        sector.code, sector.market):
                    mapping[symbol] = sector.code
            logger.info("skipped %d aggregate sectors during mapping", skipped_sectors)
            self._warn_if_sector_mapping_skewed(mapping)
            n = await asyncio.to_thread(self._store.set_sector_codes, mapping)
            logger.info("sector mapping applied to %d symbols", n)

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

            await asyncio.to_thread(self._store.finish_run, run_id, "done",
                                    total, succeeded, failed, None)
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

    def _warn_if_sector_mapping_skewed(self, mapping: dict[str, str]) -> None:
        """단일 업종코드가 매핑의 과반을 차지하면 집계 업종 누출 의심 경고.

        중단하지 않는다 — 코드/이름 필터가 놓친 케이스를 조기 발견하기 위한
        캐너리일 뿐, 휴리스틱 확정은 Phase 3 PRE-GATE에서 다룬다.
        """
        if not mapping:
            return
        code, count = Counter(mapping.values()).most_common(1)[0]
        share = count / len(mapping)
        if share > _CANARY_SHARE_THRESHOLD:
            logger.warning(
                "sector mapping canary: %s covers %.0f%% - possible aggregate leak",
                code, share * 100)

    def _set(self, run_id: int, status: str, stage: str,
             done: int, total: int, failed: int) -> None:
        self._progress = CollectionProgress(
            run_id, status, stage, done, total, failed, self._warning)
