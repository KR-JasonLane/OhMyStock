"""수집·스코어링·분석 파이프라인 스키마. Alembic 마이그레이션(최신 0005)과 1:1 정합성 유지."""

from datetime import date, datetime

from sqlalchemy import (BigInteger, Boolean, Date, DateTime, Float, ForeignKey,
                        Integer, String, Text, literal)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# instruments.state / instruments.audit_info 칼럼 길이 — 모델과 store의 절단
# 로직(collection_store.upsert_instruments)이 같은 값을 참조하도록 상수화.
# 마이그레이션 파일(0003)은 Alembic 스냅샷 관례상 리터럴 숫자를 그대로 둔다.
INSTRUMENT_STATE_MAX_LEN = 128
INSTRUMENT_AUDIT_INFO_MAX_LEN = 32

# analysis_news.title / analysis_news.url 칼럼 길이 — 모델과 store의 절단
# 로직(analysis_store._save_news)이 같은 값을 참조하도록 상수화 (T2 SSOT 관례).
# 마이그레이션 파일(0005)은 Alembic 스냅샷 관례상 리터럴 숫자를 그대로 둔다.
ANALYSIS_NEWS_TITLE_MAX_LEN = 256
ANALYSIS_NEWS_URL_MAX_LEN = 512


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


class AnalysisRunRow(Base):
    __tablename__ = "analysis_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    # 의도적 non-CASCADE — 스코어 런 정리(retention)가 분석 감사 이력을
    # 연쇄 삭제하면 안 됨. retention 설계 시 RESTRICT 제약 선검토 필요
    # (T4 아키텍트 리뷰).
    score_run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("score_runs.id"))
    model: Mapped[str] = mapped_column(String(64))
    prompt_hash: Mapped[str] = mapped_column(String(16))
    config: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    regime: Mapped[str | None] = mapped_column(String(16), nullable=True)
    market_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # nullable인 이유: 성공 런은 economist가 항상 advice를 내므로 값 자체는
    # 늘 존재하지만(폴백이어도 max_picks로 채워짐, parsing.neutral_fallback),
    # 실패 런은 economist 단계에 도달조차 못 하고 끝나는 경우가 있어(gate
    # 단계 실패 등) 그런 런에는 값이 없다 — None은 "0건 권고"와 구분되는
    # "파이프라인 미도달"을 뜻한다 (T1, P4 트레이더 패널: advice가 저장되지
    # 않아 "approve는 있는데 picks가 비어도" DB만으로 감사 불가했던 문제).
    max_picks_advice: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # economist 파싱 실패 폴백(중립+상한5, 결정 #23) 발동 여부 — 폴백을 열어둔
    # 채 유지하기로 해 감사 중요도 상승(P5 이월, 마이그레이션 0008)
    economist_fallback: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0")


class AnalysisVerdictRow(Base):
    __tablename__ = "analysis_verdicts"
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("analysis_runs.id", ondelete="CASCADE"), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    verdict: Mapped[str] = mapped_column(String(8))
    confidence: Mapped[float] = mapped_column(Float)
    reasons: Mapped[str] = mapped_column(Text)
    risk_flags: Mapped[str] = mapped_column(Text)
    picked: Mapped[bool] = mapped_column(Boolean)
    pick_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AnalysisNewsRow(Base):
    __tablename__ = "analysis_news"
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("analysis_runs.id", ondelete="CASCADE"), primary_key=True)
    scope: Mapped[str] = mapped_column(String(12), primary_key=True)
    url: Mapped[str] = mapped_column(String(ANALYSIS_NEWS_URL_MAX_LEN), primary_key=True)
    title: Mapped[str] = mapped_column(String(ANALYSIS_NEWS_TITLE_MAX_LEN))
    published_at: Mapped[str] = mapped_column(String(64))


# --- P5 트레이딩 (마이그레이션 0007, 스펙 §9) ---
# 전 FK 비-CASCADE(RESTRICT 기본) — 실거래 주문/체결 이력은 최고 등급 감사
# 자산이라 어떤 정리 작업도 연쇄 삭제하면 안 된다(AnalysisRunRow.score_run_id
# 관례와 정합, 아키텍트 패널 §9).


class TradeRunRow(Base):
    __tablename__ = "trade_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    config: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    # 킬 스위치 감사(§9, 보안 패널) — 사고 시 "언제/어떤 모드로 멈췄나"가
    # 첫 질문이다. _run() 종료 시 request_stop 여부·모드로부터 기록(Task 7).
    stopped_by_kill_switch: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0")
    kill_switch_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class TradePositionRow(Base):
    __tablename__ = "trade_positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("trade_runs.id"))
    symbol: Mapped[str] = mapped_column(String(12))  # 기존 InstrumentRow 관례
    name: Mapped[str] = mapped_column(String(64))
    market: Mapped[str] = mapped_column(String(8))   # kospi|kosdaq|etf — 비용 계산
    state: Mapped[str] = mapped_column(String(16), index=True)  # PositionState.value
    entry_phase: Mapped[str | None] = mapped_column(String(20), nullable=True)
    exit_phase: Mapped[str | None] = mapped_column(String(20), nullable=True)
    entry_price: Mapped[int] = mapped_column(Integer)  # 원 단위(G3 실측 확정)
    quantity: Mapped[int] = mapped_column(Integer)
    # 트레일링 상태 — DB 영속(§6-2: 재시작 시 확보 수익 보호 리셋 방지)
    peak_price: Mapped[int] = mapped_column(Integer)
    trailing_active: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0")
    exit_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    realized_pnl: Mapped[int | None] = mapped_column(Integer, nullable=True)
    entered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class TradeOrderRow(Base):
    __tablename__ = "trade_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("trade_runs.id"))
    # 이 주문이 속한 포지션(개발자 델타 — reconcile 분기 ②가 symbol 매칭이
    # 아니라 명시적 연결로 판단, realized_pnl이 포지션→주문 조회 가능).
    # nullable: 접수 실패 주문은 포지션 미연결일 수 있다.
    trade_position_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("trade_positions.id"), nullable=True)
    order_no: Mapped[str] = mapped_column(String(32), index=True)  # ord_no(G2 실측)
    symbol: Mapped[str] = mapped_column(String(12))  # 기존 관례
    side: Mapped[str] = mapped_column(String(4))        # buy|sell
    order_style: Mapped[str] = mapped_column(String(8))  # limit|market
    req_price: Mapped[int] = mapped_column(Integer)     # 시장가면 0
    req_qty: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    # 응답 **바디만**(JSON 텍스트) — Authorization 헤더/토큰은 어느 계층에도
    # 저장·로그 금지(§9 보안 C1)
    resp_body: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 상태 전이 시각(submitted→cancelled/filled 등) — 감사 재구성용(아키텍트 T5)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class TradeFillRow(Base):
    __tablename__ = "trade_fills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("trade_orders.id"))
    fill_price: Mapped[int] = mapped_column(Integer)
    fill_qty: Mapped[int] = mapped_column(Integer)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
