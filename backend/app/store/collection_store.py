"""수집 파이프라인 영속화. 동기 SQLAlchemy — 서비스가 asyncio.to_thread로 호출한다.
upsert는 dialect별 INSERT..ON CONFLICT (테스트=sqlite, 운영=postgresql)."""

import logging
from collections.abc import Callable, Iterable
from datetime import date, datetime, timezone

from sqlalchemy import Engine, delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.domain.broker import Candle, Instrument, Sector
from app.store.models import (INSTRUMENT_AUDIT_INFO_MAX_LEN,
                              INSTRUMENT_STATE_MAX_LEN, CandleRow,
                              CollectionRunRow, InstrumentRow,
                              SectorMembershipRow, SectorRow)

logger = logging.getLogger(__name__)


def _upsert(session: Session, model, rows: list[dict], index_elements: list[str]) -> None:
    if not rows:
        return
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise NotImplementedError(f"upsert not supported for dialect {dialect_name}")
    stmt = insert(model).values(rows)
    update_cols = {c: stmt.excluded[c] for c in rows[0] if c not in index_elements}
    session.execute(stmt.on_conflict_do_update(
        index_elements=index_elements, set_=update_cols))


class CollectionStore:
    def __init__(self, engine: Engine,
                 now: Callable[[], datetime] | None = None) -> None:
        self._sessions = sessionmaker(bind=engine)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def upsert_sectors(self, sectors: Iterable[Sector],
                       group_types: dict[str, str] | None = None) -> None:
        """group_types: code → group_type (도메인 분류 맵이 결정 — store는 무지).

        Optional인 이유: domain/collection.py가 Task 3 전환 전까지 1-인자로
        호출한다 — 필수 인자면 중간 상태에서 수집 파이프라인 전체가
        TypeError로 크래시(라이브 회귀)한다. None이면 모든 섹터가
        "unclassified"로 저장된다."""
        group_types = group_types or {}
        rows = [{"code": s.code, "market": s.market, "name": s.name,
                 "group_type": group_types.get(s.code, "unclassified")}
                for s in sectors]
        with self._sessions.begin() as session:
            _upsert(session, SectorRow, rows, ["code"])

    def upsert_instruments(self, instruments: Iterable[Instrument]) -> None:
        now = self._now()
        # pg는 VARCHAR 초과 시 예외 — 수집 전체 실패 방지용 방어적 절단
        rows = []
        truncated_symbols = []
        for i in instruments:
            if len(i.state) > INSTRUMENT_STATE_MAX_LEN or \
                    len(i.audit_info) > INSTRUMENT_AUDIT_INFO_MAX_LEN:
                truncated_symbols.append(i.symbol)
            rows.append({"symbol": i.symbol, "name": i.name, "market": i.market,
                        "instrument_type": i.instrument_type,
                        "state": i.state[:INSTRUMENT_STATE_MAX_LEN],
                        "audit_info": i.audit_info[:INSTRUMENT_AUDIT_INFO_MAX_LEN],
                        "is_active": True, "updated_at": now})
        if truncated_symbols:
            # 조용한 데이터 손실 방지 — 절단 발생 시 배치당 1회 경고(개수 + 표본)
            logger.warning(
                "instrument state/audit_info truncated for %d symbols (sample: %s)",
                len(truncated_symbols), sorted(truncated_symbols)[:10])
        with self._sessions.begin() as session:
            _upsert(session, InstrumentRow, rows, ["symbol"])

    def replace_sector_memberships(self, memberships: dict[str, list[str]]) -> int:
        """업종 소속 전체 교체 (delete-and-insert, 단일 트랜잭션).

        전체 교체인 이유: 소속은 편출입이 있는 스냅샷 데이터라 이전 실행의
        소속이 남으면 안 된다. instruments에 없는 symbol과 sectors에 없는
        sector_code는 각각 스킵하고 경고 (FK 위반으로 트랜잭션 전체가
        롤백되는 것을 방지 — 정규화 차이/신규 상장·업종 타이밍 및 브로커
        응답 페이지네이션 중복). 반환: 삽입 행 수."""
        with self._sessions.begin() as session:
            all_symbols = {s for members in memberships.values() for s in members}
            known_symbols = set(session.scalars(
                select(InstrumentRow.symbol)
                .where(InstrumentRow.symbol.in_(all_symbols))))
            unknown_symbols = all_symbols - known_symbols
            if unknown_symbols:
                logger.warning(
                    "sector memberships skipped for %d unknown symbols (sample: %s)",
                    len(unknown_symbols), sorted(unknown_symbols)[:10])

            known_sectors = set(session.scalars(
                select(SectorRow.code)
                .where(SectorRow.code.in_(memberships.keys()))))
            unknown_sectors = set(memberships.keys()) - known_sectors
            if unknown_sectors:
                logger.warning(
                    "sector memberships skipped for %d unknown sector codes (sample: %s)",
                    len(unknown_sectors), sorted(unknown_sectors)[:10])

            # 브로커 응답 중복(페이지네이션 등)이 PK 위반으로 전체 롤백을
            # 일으키지 않도록 (sector_code, symbol) 쌍을 삽입 전 dedup —
            # dict.fromkeys로 first-seen 순서를 보존한다.
            pairs = dict.fromkeys(
                (code, s)
                for code, members in memberships.items()
                if code in known_sectors
                for s in members if s in known_symbols)
            rows = [{"sector_code": code, "symbol": s} for code, s in pairs]
            session.execute(delete(SectorMembershipRow))
            if rows:
                # 매 실행 전체 교체라 upsert 불필요 — 대량 bulk insert 경로
                session.execute(SectorMembershipRow.__table__.insert(), rows)
            return len(rows)

    def upsert_candles(self, candles: Iterable[Candle]) -> None:
        rows = [{"symbol": c.symbol, "date": c.date, "open": c.open, "high": c.high,
                 "low": c.low, "close": c.close, "volume": c.volume} for c in candles]
        with self._sessions.begin() as session:
            _upsert(session, CandleRow, rows, ["symbol", "date"])

    def latest_candle_date(self, symbol: str) -> date | None:
        """종목의 최신 봉 일자. 단건 조회 — 벌크 경로는 `latest_candle_dates` 참고.

        불변식: 수집은 항상 고정 윈도우(600봉) 전체를 재수집해 upsert하고,
        스킵 여부는 이 날짜를 달력 기준일(`market_calendar.previous_weekday`,
        `CollectionService.reference_provider`)과 비교해서만 판단한다 — 이
        값 자체를 '이후만 증분 수집'하는 커서로 오용하면, 예외 없이 부분
        반환된 런의 중간 구멍이 영구화된다 (자가치유 특성 상실). 증분 수집으로
        바꾸려면 갭 탐지부터 추가할 것.
        """
        with self._sessions() as session:
            return session.scalar(select(func.max(CandleRow.date))
                                  .where(CandleRow.symbol == symbol))

    def latest_candle_dates(self) -> dict[str, date]:
        """전 종목 최신 봉 일자 일괄 조회 — 단일 GROUP BY 쿼리.

        수집 서비스가 종목마다 latest_candle_date를 왕복 호출(N+1)하지 않도록
        candles 단계 시작 시 1회 호출해 dict로 조회하고, 러닝 중 1회 고정한
        달력 기준일과 종목별로 비교해 스킵을 판단하는 용도 (`CollectionService`
        참고). 위 `latest_candle_date`와 동일한 불변식 — 증분 커서로 쓰지 말 것.
        """
        with self._sessions() as session:
            rows = session.execute(
                select(CandleRow.symbol, func.max(CandleRow.date))
                .group_by(CandleRow.symbol)
            ).all()
            return {symbol: latest for symbol, latest in rows}

    def list_symbols(self) -> list[str]:
        with self._sessions() as session:
            return list(session.scalars(select(InstrumentRow.symbol)
                                        .where(InstrumentRow.is_active.is_(True))
                                        .order_by(InstrumentRow.symbol)))

    def create_run(self) -> int:
        with self._sessions.begin() as session:
            run = CollectionRunRow(started_at=self._now(), status="running")
            session.add(run)
            session.flush()
            return run.id

    def deactivate_missing(self, seen_symbols: set[str]) -> int:
        """Mark instruments not in seen_symbols as inactive. Returns count of deactivated symbols."""
        with self._sessions.begin() as session:
            result = session.execute(update(InstrumentRow)
                                     .where(~InstrumentRow.symbol.in_(seen_symbols),
                                            InstrumentRow.is_active.is_(True))
                                     .values(is_active=False))
            return result.rowcount

    def finish_run(self, run_id: int, status: str, total: int, succeeded: int,
                   failed: int, error_summary: str | None = None) -> None:
        with self._sessions.begin() as session:
            result = session.execute(update(CollectionRunRow)
                                     .where(CollectionRunRow.id == run_id)
                                     .values(finished_at=self._now(), status=status,
                                             total_symbols=total, succeeded=succeeded,
                                             failed=failed, error_summary=error_summary))
            if result.rowcount == 0:
                logger.warning("finish_run: run %s not found", run_id)
