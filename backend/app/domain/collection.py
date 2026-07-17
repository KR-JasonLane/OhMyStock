"""수집 오케스트레이션. BrokerPort와 Store 메서드 계약만 안다 (키움/SQL 무지).
Store 호출은 동기이므로 asyncio.to_thread로 이벤트 루프를 막지 않는다."""

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date

from app.domain.broker import BrokerPort
from app.domain.errors import AuthError, BrokerError, RateLimitError

logger = logging.getLogger(__name__)

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


class CollectionService:
    def __init__(self, broker: BrokerPort, store,
                 markets: tuple[str, ...] = ("kospi", "kosdaq", "etf"),
                 candle_count: int = 600,
                 max_consecutive_failures: int = 20) -> None:
        """markets: 수집 대상 시장 목록.

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
        self._running = False
        self._progress: CollectionProgress | None = None

    def is_running(self) -> bool:
        return self._running

    def progress(self) -> CollectionProgress | None:
        return self._progress

    async def run(self) -> None:
        """전체 수집 파이프라인을 실행한다 (instruments → sectors → candles).

        정규장(09:00-15:30 KST) 종료 후 실행해야 한다 — 장중 실행 시 미확정
        당일 봉이 저장되고, latest_candle_date 기반 스킵 로직이 이 미확정 봉을
        "이미 최신"으로 오판해 고착시킬 수 있다. 실행 시각 강제는 이 서비스의
        책임이 아니라 Phase 6 스케줄러가 진다 (거래일 캘린더가 필요하기 때문).
        """
        if self._running:
            raise RuntimeError("collection already running")
        self._running = True
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
            reference_date: date | None = None
            consecutive = 0
            for i, symbol in enumerate(symbols, start=1):
                latest = latest_dates.get(symbol)
                # reference_date가 아직 없다면(런 시작 직후) 스킵하지 않고 실제
                # 조회한다 — 이번 런에서 최소 1건은 조회해야 reference_date가
                # 확정되어 이후 종목들의 스킵 판단 기준이 생긴다.
                if (reference_date is not None and latest is not None
                        and latest >= reference_date):
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
                            if reference_date is None:
                                reference_date = max(c.date for c in candles)
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
        self._progress = CollectionProgress(run_id, status, stage, done, total, failed)
