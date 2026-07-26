"""관리매매 성과를 노출하는 조회 전용 dashboard API."""

import logging
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, field_serializer
from starlette.responses import JSONResponse

from app.core.market_calendar import KST
from app.domain.dashboard.models import DashboardPeriod


router = APIRouter()
logger = logging.getLogger(__name__)
_DEFAULT_PERIOD_DAYS = 30
_MAX_PERIOD_DAYS = 366
_RECENT_TRADES_LIMIT = 100


class _ResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DashboardPeriodResponse(_ResponseModel):
    start: date
    end: date
    timezone: Literal["Asia/Seoul"]


class DashboardSummaryResponse(_ResponseModel):
    realized_pnl: int | None
    realized_pnl_status: Literal["complete", "partial", "unavailable"]
    unrealized_pnl: int | None
    unrealized_pnl_status: Literal["complete", "partial", "unavailable"]
    total_pnl: int | None
    total_pnl_status: Literal["complete", "partial", "unavailable"]
    realized_return_pct: Decimal | None
    closed_trade_count: int
    incomplete_closed_trade_count: int
    wins: int
    losses: int
    draws: int
    win_rate: Decimal | None
    cost_basis: Literal["recorded", "estimated", "unavailable"]

    @field_serializer("realized_return_pct", "win_rate", when_used="json")
    def _decimal_as_json_number(self, value: Decimal | None) -> float | None:
        return float(value) if value is not None else None


class EquityPointResponse(_ResponseModel):
    position_id: int
    closed_at: AwareDatetime
    realized_pnl: int
    cumulative_realized_pnl: int


class DashboardPositionResponse(_ResponseModel):
    position_id: int
    symbol: str
    name: str
    entry_price: int
    quantity: int
    entered_at: AwareDatetime | None
    mark_price: int | None
    marked_at: AwareDatetime | None
    unrealized_pnl: int | None
    valuation_status: Literal["complete", "partial", "unavailable"]


class RecentTradeResponse(_ResponseModel):
    position_id: int
    symbol: str
    name: str
    entry_price: int
    quantity: int
    exit_price: int | None
    realized_pnl: int | None
    closed_at: AwareDatetime
    exit_reason: str | None


class DashboardFreshnessResponse(_ResponseModel):
    as_of: AwareDatetime
    mark_stale_after_seconds: int
    latest_marked_at: AwareDatetime | None


class DashboardWarningsResponse(_ResponseModel):
    corrupted_row_count: int
    incomplete_closed_trade_count: int


class DashboardOverviewResponse(_ResponseModel):
    period: DashboardPeriodResponse
    summary: DashboardSummaryResponse
    equity_curve: tuple[EquityPointResponse, ...]
    positions: tuple[DashboardPositionResponse, ...]
    recent_trades: tuple[RecentTradeResponse, ...]
    freshness: DashboardFreshnessResponse
    warnings: DashboardWarningsResponse


class DashboardErrorResponse(BaseModel):
    code: Literal["dashboard_unavailable"]


def _period_or_422(from_date: date | None, to_date: date | None,
                   timezone: Literal["Asia/Seoul"]) -> DashboardPeriod:
    if from_date is None and to_date is None:
        end = datetime.now(KST).date()
        start = end - timedelta(days=_DEFAULT_PERIOD_DAYS - 1)
    elif from_date is None or to_date is None:
        raise HTTPException(status_code=422, detail="from and to must be paired")
    else:
        start, end = from_date, to_date
    if start > end:
        raise HTTPException(status_code=422, detail="from must not be after to")
    if (end - start).days + 1 > _MAX_PERIOD_DAYS:
        raise HTTPException(status_code=422, detail="dashboard period is too long")
    return DashboardPeriod(start, end, timezone)


@router.get(
    "/dashboard/overview",
    response_model=DashboardOverviewResponse,
    responses={503: {"model": DashboardErrorResponse}},
)
def dashboard_overview(
        request: Request,
        from_date: Annotated[date | None, Query(alias="from")] = None,
        to_date: Annotated[date | None, Query(alias="to")] = None,
        timezone: Literal["Asia/Seoul"] = "Asia/Seoul",
) -> DashboardOverviewResponse | JSONResponse:
    """현재 실행 환경의 관리 포지션 SQL 읽기 모델만 반환한다."""
    period = _period_or_422(from_date, to_date, timezone)
    try:
        overview = request.app.state.dashboard_store.overview(
            period, request.app.state.settings.run_environment, datetime.now(KST))
        bounded_overview = replace(
            overview, recent_trades=overview.recent_trades[:_RECENT_TRADES_LIMIT])
        return DashboardOverviewResponse.model_validate(bounded_overview)
    except Exception as exc:  # noqa: BLE001 - 외부 계약은 안정된 코드만 노출한다.
        logger.warning("dashboard overview unavailable: %s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content=DashboardErrorResponse(code="dashboard_unavailable").model_dump(),
        )
