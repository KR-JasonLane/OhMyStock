"""수집 파이프라인 스키마. Alembic 0002_market_data.py와 1:1 정합성 유지."""

from datetime import date, datetime

from sqlalchemy import (BigInteger, Boolean, Date, DateTime, ForeignKey, Integer,
                        String, Text, literal)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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
    state: Mapped[str] = mapped_column(String(128), default="", server_default="")
    audit_info: Mapped[str] = mapped_column(String(32), default="", server_default="")
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
