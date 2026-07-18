"""수집 파이프라인 영속화. 동기 SQLAlchemy — 서비스가 asyncio.to_thread로 호출한다.
upsert는 dialect별 INSERT..ON CONFLICT (테스트=sqlite, 운영=postgresql)."""

import logging
from collections.abc import Callable, Iterable
from datetime import date, datetime, timezone

from sqlalchemy import Engine, delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.domain.broker import Candle, Instrument, Sector
from app.store.models import (CandleRow, CollectionRunRow, InstrumentRow,
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
                       group_types: dict[str, str]) -> None:
        """group_types: code → group_type (도메인 분류 맵이 결정 — store는 무지)."""
        rows = [{"code": s.code, "market": s.market, "name": s.name,
                 "group_type": group_types.get(s.code, "unclassified")}
                for s in sectors]
        with self._sessions.begin() as session:
            _upsert(session, SectorRow, rows, ["code"])

    def upsert_instruments(self, instruments: Iterable[Instrument]) -> None:
        now = self._now()
        # pg는 VARCHAR 초과 시 예외 — 수집 전체 실패 방지용 방어적 절단
        rows = [{"symbol": i.symbol, "name": i.name, "market": i.market,
                 "instrument_type": i.instrument_type, "state": i.state[:128],
                 "audit_info": i.audit_info[:32], "is_active": True,
                 "updated_at": now} for i in instruments]
        with self._sessions.begin() as session:
            _upsert(session, InstrumentRow, rows, ["symbol"])

    def set_sector_codes(self, mapping: dict[str, str]) -> int:
        """Deprecated — Task 3에서 replace_sector_memberships로 전환하며 제거된다.
        sector_code 칼럼은 마이그레이션 0003에서 삭제됨 (손상 데이터, 소비자 없음)."""
        logger.warning("set_sector_codes is deprecated and now a no-op (removed in Task 3)")
        return 0

    def replace_sector_memberships(self, memberships: dict[str, list[str]]) -> int:
        """업종 소속 전체 교체 (delete-and-insert, 단일 트랜잭션).

        전체 교체인 이유: 소속은 편출입이 있는 스냅샷 데이터라 이전 실행의
        소속이 남으면 안 된다. instruments에 없는 symbol은 스킵하고 경고
        (FK 위반 방지 — 정규화 차이/신규 상장 타이밍). 반환: 삽입 행 수."""
        with self._sessions.begin() as session:
            all_symbols = {s for members in memberships.values() for s in members}
            known = set(session.scalars(
                select(InstrumentRow.symbol)
                .where(InstrumentRow.symbol.in_(all_symbols))))
            unknown = len(all_symbols - known)
            if unknown:
                logger.warning(
                    "sector memberships skipped for %d unknown symbols", unknown)
            rows = [{"sector_code": code, "symbol": s}
                    for code, members in memberships.items()
                    for s in members if s in known]
            session.execute(delete(SectorMembershipRow))
            if rows:
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
