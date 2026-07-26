"""대시보드 전용 SQL 읽기 모델.

브로커·주문 경로에 의존하지 않고, 영속된 관리 포지션과 실행 환경만 명시적으로
join한다. 원시 브로커 응답과 계좌 식별자는 선택하지 않는다.
"""

from datetime import datetime, timedelta

from sqlalchemy import Engine, and_, or_, select
from sqlalchemy.orm import sessionmaker

from app.domain.dashboard.models import (ClosedPosition, DashboardOverview,
                                         DashboardPeriod, OpenPosition,
                                         build_dashboard_overview)
from app.domain.trading.models import ExitReason, PositionState
from app.store.kst_time import as_aware_utc, coarse_utc_bounds
from app.store.models import TradePositionRow, TradeRunRow


_DASHBOARD_OPEN_STATES = frozenset({
    PositionState.ENTERED.value,
    PositionState.EXITING.value,
    PositionState.EXIT_FAILED.value,
})
_KNOWN_POSITION_STATES = frozenset(state.value for state in PositionState)
_KNOWN_EXIT_REASONS = frozenset(reason.value for reason in ExitReason)
_MARK_STALE_AFTER = timedelta(minutes=10)


class DashboardStore:
    """관리매매 성과만 조립하는 SQL 읽기 모델."""

    def __init__(self, engine: Engine):
        self._sessions = sessionmaker(bind=engine)

    def overview(self, period: DashboardPeriod, run_environment: str,
                 now: datetime) -> DashboardOverview:
        """환경별 행을 읽고, KST 날짜를 Python에서 다시 정확히 판정한다."""
        lo, _ = coarse_utc_bounds(period.start)
        _, hi = coarse_utc_bounds(period.end)
        columns = (
            TradePositionRow.id,
            TradePositionRow.symbol,
            TradePositionRow.name,
            TradePositionRow.state,
            TradePositionRow.entry_price,
            TradePositionRow.quantity,
            TradePositionRow.exit_price,
            TradePositionRow.exit_reason,
            TradePositionRow.realized_pnl,
            TradePositionRow.entered_at,
            TradePositionRow.closed_at,
            TradePositionRow.mark_price,
            TradePositionRow.marked_at,
        )
        statement = (
            select(*columns)
            .select_from(TradePositionRow)
            .join(TradeRunRow,
                  TradePositionRow.trade_run_id == TradeRunRow.id)
            .where(
                TradeRunRow.run_environment == run_environment,
                or_(
                    # 비종결 상태는 closed_at 오염도 경고해야 하므로 모두 읽는다.
                    TradePositionRow.state != PositionState.CLOSED.value,
                    and_(
                        TradePositionRow.state == PositionState.CLOSED.value,
                        or_(
                            TradePositionRow.closed_at.is_(None),
                            and_(TradePositionRow.closed_at >= lo,
                                 TradePositionRow.closed_at <= hi),
                        ),
                    ),
                ),
            )
        )
        with self._sessions() as session:
            rows = session.execute(statement).mappings().all()

        closed: list[ClosedPosition] = []
        open_: list[OpenPosition] = []
        corrupted = 0
        for row in rows:
            state = row["state"]
            if state not in _KNOWN_POSITION_STATES:
                corrupted += 1
                continue
            closed_at = _aware_or_none(row["closed_at"])
            if state == PositionState.CLOSED.value:
                if closed_at is None:
                    corrupted += 1
                    continue
                # coarse range는 SQL 스캔 상한일 뿐, 귀속의 진실은 KST 날짜다.
                if not period.includes(closed_at):
                    continue
                position = _closed_position(row, closed_at)
                if position is None:
                    corrupted += 1
                else:
                    closed.append(position)
                continue
            if state not in _DASHBOARD_OPEN_STATES:
                # pending_entry·entry_failed는 보유 중인 관리 포지션이 아니지만,
                # 이 상태에 closed_at이 있으면 수명주기 손상으로 드러낸다.
                if closed_at is not None:
                    corrupted += 1
                continue
            if closed_at is not None:
                corrupted += 1
                continue
            position = _open_position(row)
            if position is None:
                corrupted += 1
            else:
                open_.append(position)

        return build_dashboard_overview(
            period, closed, open_, now=now, corrupted_row_count=corrupted,
            mark_stale_after=_MARK_STALE_AFTER)


def _closed_position(row, closed_at: datetime) -> ClosedPosition | None:
    if not _valid_position_values(row):
        return None
    exit_price = row["exit_price"]
    if not _positive_int(exit_price):
        return None
    exit_reason = row["exit_reason"]
    if exit_reason is not None and exit_reason not in _KNOWN_EXIT_REASONS:
        return None
    realized_pnl = row["realized_pnl"]
    if realized_pnl is not None and not _int_value(realized_pnl):
        return None
    return ClosedPosition(
        row["id"], row["symbol"], row["name"], row["entry_price"],
        row["quantity"], exit_price, realized_pnl, closed_at, exit_reason)


def _open_position(row) -> OpenPosition | None:
    if not _valid_position_values(row):
        return None
    mark_price = row["mark_price"]
    marked_at = _aware_or_none(row["marked_at"])
    if mark_price is None and marked_at is None:
        pass
    elif not _positive_int(mark_price) or marked_at is None:
        return None
    return OpenPosition(
        row["id"], row["symbol"], row["name"], row["entry_price"],
        row["quantity"], _aware_or_none(row["entered_at"]), mark_price, marked_at)


def _valid_position_values(row) -> bool:
    return (
        _positive_int(row["id"])
        and isinstance(row["symbol"], str) and bool(row["symbol"])
        and isinstance(row["name"], str) and bool(row["name"])
        and _positive_int(row["entry_price"])
        and _positive_int(row["quantity"])
    )


def _positive_int(value) -> bool:
    return _int_value(value) and value > 0


def _int_value(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _aware_or_none(value) -> datetime | None:
    if value is None or not isinstance(value, datetime):
        return None
    return as_aware_utc(value)
