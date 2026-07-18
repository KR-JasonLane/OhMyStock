"""수집·스코어링 파이프라인 스키마. Alembic 마이그레이션(최신 0004)과 1:1 정합성 유지."""

from datetime import date, datetime

from sqlalchemy import (BigInteger, Boolean, Date, DateTime, Float, ForeignKey,
                        Integer, String, Text, literal)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# instruments.state / instruments.audit_info 칼럼 길이 — 모델과 store의 절단
# 로직(collection_store.upsert_instruments)이 같은 값을 참조하도록 상수화.
# 마이그레이션 파일(0003)은 Alembic 스냅샷 관례상 리터럴 숫자를 그대로 둔다.
INSTRUMENT_STATE_MAX_LEN = 128
INSTRUMENT_AUDIT_INFO_MAX_LEN = 32


class Base(DeclarativeBase):
    pass


class SectorRow(Base):
    __tablename__ = "sectors"
    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    market: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(64))
    group_type: Mapped[str] = mapped_column(
        String(24), default="unclassified", server_default="unclassified")


class SectorMembershipRow(Base):
    __tablename__ = "sector_memberships"
    sector_code: Mapped[str] = mapped_column(
        String(8), ForeignKey("sectors.code"), primary_key=True)
    symbol: Mapped[str] = mapped_column(
        String(12), ForeignKey("instruments.symbol"), primary_key=True)


class InstrumentRow(Base):
    __tablename__ = "instruments"
    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    market: Mapped[str] = mapped_column(String(16))
    instrument_type: Mapped[str] = mapped_column(String(32), default="", server_default="")
    state: Mapped[str] = mapped_column(
        String(INSTRUMENT_STATE_MAX_LEN), default="", server_default="")
    audit_info: Mapped[str] = mapped_column(
        String(INSTRUMENT_AUDIT_INFO_MAX_LEN), default="", server_default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=literal(True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CandleRow(Base):
    __tablename__ = "candles"
    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[int] = mapped_column(Integer)
    high: Mapped[int] = mapped_column(Integer)
    low: Mapped[int] = mapped_column(Integer)
    close: Mapped[int] = mapped_column(Integer)
    volume: Mapped[int] = mapped_column(BigInteger)


class CollectionRunRow(Base):
    __tablename__ = "collection_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    total_symbols: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    succeeded: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class ScoreRunRow(Base):
    __tablename__ = "score_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    reference_date: Mapped[date] = mapped_column(Date)
    universe_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    stale_excluded: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")


class ScoreSectorRow(Base):
    __tablename__ = "score_sectors"
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("score_runs.id", ondelete="CASCADE"), primary_key=True)
    sector_code: Mapped[str] = mapped_column(String(8), primary_key=True)
    r20: Mapped[float] = mapped_column(Float)
    r60: Mapped[float] = mapped_column(Float)
    r5: Mapped[float] = mapped_column(Float)
    score: Mapped[float] = mapped_column(Float)
    rank: Mapped[int] = mapped_column(Integer)
    selected: Mapped[bool] = mapped_column(Boolean)


class ScoreRow(Base):
    """total_score = final_weight_sector×sector_score +
    final_weight_strategy×strategy_score_norm 공식이 저장값만으로 재현된다.
    strategy_score는 정규화 전 원값(참고용, 후보 간 비교 불가)."""
    __tablename__ = "scores"
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("score_runs.id", ondelete="CASCADE"), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer)
    total_score: Mapped[float] = mapped_column(Float)
    sector_code: Mapped[str] = mapped_column(String(8))
    sector_score: Mapped[float] = mapped_column(Float)
    strategy_score: Mapped[float] = mapped_column(Float)
    strategy_score_norm: Mapped[float] = mapped_column(Float)


class ScoreDetailRow(Base):
    __tablename__ = "score_details"
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("score_runs.id", ondelete="CASCADE"), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    strategy: Mapped[str] = mapped_column(String(32), primary_key=True)
    signal: Mapped[bool] = mapped_column(Boolean)
    avg_return: Mapped[float] = mapped_column(Float)
    win_rate: Mapped[float] = mapped_column(Float)
    occurrences: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
