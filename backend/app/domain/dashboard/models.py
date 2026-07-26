"""관리 포지션 성과를 계산하는 프레임워크 독립 읽기 모델.

이 모듈은 SQL 행·API DTO·브로커 응답을 모른다. store가 검증한 관리 포지션
스냅샷만 받아, 기간 귀속·손익·신선도 상태를 일관되게 계산한다.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo


Completeness = Literal["complete", "partial", "unavailable"]
CostBasis = Literal["recorded", "estimated", "unavailable"]


@dataclass(frozen=True)
class DashboardPeriod:
    start: date
    end: date
    timezone: str

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("dashboard period start must not be after end")
        try:
            ZoneInfo(self.timezone)
        except Exception as exc:
            raise ValueError(f"unknown dashboard timezone: {self.timezone}") from exc

    def includes(self, instant: datetime) -> bool:
        if instant.tzinfo is None or instant.utcoffset() is None:
            return False
        local_day = instant.astimezone(ZoneInfo(self.timezone)).date()
        return self.start <= local_day <= self.end


@dataclass(frozen=True)
class ClosedPosition:
    """청산된 하나의 관리 포지션 생애주기."""

    position_id: int
    symbol: str
    name: str
    entry_price: int
    quantity: int
    exit_price: int | None
    realized_pnl: int | None
    closed_at: datetime
    exit_reason: str | None


@dataclass(frozen=True)
class OpenPosition:
    """현재 열려 있는 하나의 관리 포지션과 마지막 저장 mark."""

    position_id: int
    symbol: str
    name: str
    entry_price: int
    quantity: int
    entered_at: datetime | None
    mark_price: int | None
    marked_at: datetime | None


@dataclass(frozen=True)
class EquityPoint:
    position_id: int
    closed_at: datetime
    realized_pnl: int
    cumulative_realized_pnl: int


@dataclass(frozen=True)
class DashboardPosition:
    position_id: int
    symbol: str
    name: str
    entry_price: int
    quantity: int
    entered_at: datetime | None
    mark_price: int | None
    marked_at: datetime | None
    unrealized_pnl: int | None
    valuation_status: Completeness


@dataclass(frozen=True)
class RecentTrade:
    position_id: int
    symbol: str
    name: str
    entry_price: int
    quantity: int
    exit_price: int | None
    realized_pnl: int | None
    closed_at: datetime
    exit_reason: str | None


@dataclass(frozen=True)
class DashboardSummary:
    realized_pnl: int | None
    realized_pnl_status: Completeness
    unrealized_pnl: int | None
    unrealized_pnl_status: Completeness
    total_pnl: int | None
    total_pnl_status: Completeness
    realized_return_pct: Decimal | None
    closed_trade_count: int
    incomplete_closed_trade_count: int
    wins: int
    losses: int
    draws: int
    win_rate: Decimal | None
    cost_basis: CostBasis


@dataclass(frozen=True)
class DashboardFreshness:
    as_of: datetime
    mark_stale_after_seconds: int
    latest_marked_at: datetime | None


@dataclass(frozen=True)
class DashboardWarnings:
    corrupted_row_count: int
    incomplete_closed_trade_count: int


@dataclass(frozen=True)
class DashboardOverview:
    period: DashboardPeriod
    summary: DashboardSummary
    equity_curve: tuple[EquityPoint, ...]
    positions: tuple[DashboardPosition, ...]
    recent_trades: tuple[RecentTrade, ...]
    freshness: DashboardFreshness
    warnings: DashboardWarnings


def build_dashboard_overview(
        period: DashboardPeriod,
        closed_positions: list[ClosedPosition],
        open_positions: list[OpenPosition],
        *,
        now: datetime,
        corrupted_row_count: int = 0,
        mark_stale_after: timedelta = timedelta(minutes=10),
) -> DashboardOverview:
    """유효한 포지션 스냅샷을 집계한다.

    청산 성과는 `closed_at`의 기간 귀속만 따르고, 미확정 mark는 금액 0으로
    보정하지 않는다. 손상 원시 행의 제외 결정은 store가 하며 그 수는 경고로
    전달한다.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("dashboard now must be timezone-aware")
    if corrupted_row_count < 0:
        raise ValueError("corrupted row count must not be negative")
    if mark_stale_after < timedelta(0):
        raise ValueError("mark stale threshold must not be negative")

    period_closed = [position for position in closed_positions
                     if period.includes(position.closed_at)]
    complete_closed = [position for position in period_closed
                       if position.realized_pnl is not None]
    incomplete_closed_count = len(period_closed) - len(complete_closed)

    realized_values = [position.realized_pnl for position in complete_closed]
    realized_pnl = (sum(realized_values) if realized_values
                    else (0 if not incomplete_closed_count else None))
    realized_status: Completeness = (
        "partial" if incomplete_closed_count else "complete")
    if incomplete_closed_count and not complete_closed:
        realized_status = "unavailable"

    invested = sum(
        Decimal(position.entry_price) * Decimal(position.quantity)
        for position in complete_closed
        if position.entry_price > 0 and position.quantity > 0
    )
    return_pct = (
        Decimal(realized_pnl) / invested * Decimal("100")
        if realized_pnl is not None and not incomplete_closed_count and invested else None
    )
    wins = sum(position.realized_pnl > 0 for position in complete_closed)
    losses = sum(position.realized_pnl < 0 for position in complete_closed)
    draws = sum(position.realized_pnl == 0 for position in complete_closed)
    decided_trades = wins + losses
    win_rate = (Decimal(wins) / Decimal(decided_trades) * Decimal("100")
                if decided_trades else None)

    curve: list[EquityPoint] = []
    cumulative = 0
    for position in sorted(complete_closed, key=lambda item: (item.closed_at, item.position_id)):
        cumulative += position.realized_pnl  # narrowed above
        curve.append(EquityPoint(position.position_id, position.closed_at,
                                 position.realized_pnl, cumulative))

    dashboard_positions: list[DashboardPosition] = []
    usable_unrealized: list[int] = []
    latest_marks: list[datetime] = []
    for position in sorted(open_positions, key=lambda item: item.position_id):
        if _valid_mark_observation(position, now):
            latest_marks.append(position.marked_at)  # narrowed in helper
        valuation = _valuation(position, now, mark_stale_after)
        if valuation is not None:
            usable_unrealized.append(valuation)
        dashboard_positions.append(DashboardPosition(
            position.position_id, position.symbol, position.name,
            position.entry_price, position.quantity, position.entered_at,
            position.mark_price, position.marked_at, valuation,
            "complete" if valuation is not None else "unavailable"))

    unavailable_open_count = len(open_positions) - len(usable_unrealized)
    unrealized_pnl = (sum(usable_unrealized) if usable_unrealized
                      else (0 if not open_positions else None))
    unrealized_status: Completeness = "complete"
    if unavailable_open_count:
        unrealized_status = "partial" if usable_unrealized else "unavailable"

    # 거래가 전혀 없을 때의 확정손익 0은 정상 빈 합계다. 그러나 열린 포지션의
    # mark가 전부 확인 불가일 때 그 0만으로 총손익을 표시하면 평가손익 누락을
    # 숨기므로 총손익에는 포함하지 않는다.
    known_components: list[int] = []
    if realized_pnl is not None and (period_closed or not open_positions):
        known_components.append(realized_pnl)
    if unrealized_pnl is not None and (open_positions or not period_closed):
        known_components.append(unrealized_pnl)
    total_pnl = sum(known_components) if known_components else None
    total_status: Completeness = "complete"
    if (realized_status != "complete" or unrealized_status != "complete"):
        total_status = "partial" if total_pnl is not None else "unavailable"

    # 현재 실현손익은 프로젝트 비용 모델을 적용한 추정값이다. 청산 행의
    # realized_pnl 자체가 없으면 비용 계산도 수행할 수 없으므로 unavailable로
    # 격리한다. 이 구분은 브로커 비용과의 완전 대사 여부를 주장하지 않는다.
    cost_basis: CostBasis = (
        "unavailable" if incomplete_closed_count else "estimated")

    summary = DashboardSummary(
        realized_pnl, realized_status, unrealized_pnl, unrealized_status,
        total_pnl, total_status, return_pct, len(period_closed),
        incomplete_closed_count, wins, losses, draws, win_rate, cost_basis)
    recent_trades = tuple(
        RecentTrade(position.position_id, position.symbol, position.name,
                    position.entry_price, position.quantity, position.exit_price,
                    position.realized_pnl, position.closed_at, position.exit_reason)
        for position in sorted(period_closed,
                               key=lambda item: (item.closed_at, item.position_id),
                               reverse=True))
    return DashboardOverview(
        period, summary, tuple(curve), tuple(dashboard_positions), recent_trades,
        DashboardFreshness(now, int(mark_stale_after.total_seconds()),
                           max(latest_marks, default=None)),
        DashboardWarnings(corrupted_row_count, incomplete_closed_count))


def _valuation(position: OpenPosition, now: datetime,
               stale_after: timedelta) -> int | None:
    if (position.entry_price <= 0 or position.quantity <= 0
            or position.mark_price is None or position.mark_price <= 0
            or position.marked_at is None
            or position.marked_at.tzinfo is None
            or position.marked_at.utcoffset() is None
            or position.marked_at > now):
        return None
    if now - position.marked_at > stale_after:
        return None
    return (position.mark_price - position.entry_price) * position.quantity


def _valid_mark_observation(position: OpenPosition, now: datetime) -> bool:
    """시세 관측 기준시각에는 stale mark를 포함하되 미래 시각은 제외한다."""
    return (
        position.mark_price is not None and position.mark_price > 0
        and position.marked_at is not None
        and position.marked_at.tzinfo is not None
        and position.marked_at.utcoffset() is not None
        and position.marked_at <= now
    )
