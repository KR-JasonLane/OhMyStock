from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.dashboard.models import (
    ClosedPosition,
    DashboardPeriod,
    OpenPosition,
    build_dashboard_overview,
)


KST = "+09:00"
NOW = datetime(2026, 7, 26, 3, 0, tzinfo=timezone.utc)
PERIOD = DashboardPeriod(date(2026, 7, 20), date(2026, 7, 25), "Asia/Seoul")


def _closed(**overrides) -> ClosedPosition:
    values = {
        "position_id": 1,
        "symbol": "005930",
        "name": "삼성전자",
        "entry_price": 100_000,
        "quantity": 2,
        "exit_price": 105_000,
        "realized_pnl": 10_000,
        "closed_at": datetime(2026, 7, 22, 6, 0, tzinfo=timezone.utc),
        "exit_reason": "take_profit",
    }
    return ClosedPosition(**(values | overrides))


def _open(**overrides) -> OpenPosition:
    values = {
        "position_id": 10,
        "symbol": "000660",
        "name": "SK하이닉스",
        "entry_price": 100_000,
        "quantity": 2,
        "entered_at": datetime(2026, 7, 24, 1, 0, tzinfo=timezone.utc),
        "mark_price": 103_000,
        "marked_at": NOW - timedelta(minutes=3),
    }
    return OpenPosition(**(values | overrides))


def test_청산_시각이_KST_기간_안인_포지션만_진입일과_무관하게_확정성과에_포함한다():
    before_entered = _closed(
        position_id=1,
        closed_at=datetime(2026, 7, 20, 0, 5, tzinfo=timezone.utc),
    )
    after_closed = _closed(
        position_id=2,
        closed_at=datetime(2026, 7, 25, 15, 1, tzinfo=timezone.utc),
        realized_pnl=99_999,
    )

    overview = build_dashboard_overview(
        PERIOD, [before_entered, after_closed], [], now=NOW)

    assert overview.summary.realized_pnl == 10_000
    assert overview.summary.closed_trade_count == 1
    assert [point.position_id for point in overview.equity_curve] == [1]


def test_확정_성과는_포지션_생애주기당_한번만_승패보합과_수익률을_집계한다():
    # 같은 position_id의 분할 주문·체결은 store가 아닌 포지션 행 하나로 이미
    # 물질화됐다. 이 회귀는 order/fill 행 수가 거래 수가 되는 퇴행을 막는다.
    wins_losses_draws = [
        _closed(position_id=7, realized_pnl=10_000, entry_price=100_000, quantity=2),
        _closed(position_id=8, realized_pnl=-5_000, entry_price=50_000, quantity=2,
                closed_at=datetime(2026, 7, 23, 6, 0, tzinfo=timezone.utc)),
        _closed(position_id=9, realized_pnl=0, entry_price=25_000, quantity=4,
                closed_at=datetime(2026, 7, 24, 6, 0, tzinfo=timezone.utc)),
    ]

    overview = build_dashboard_overview(PERIOD, wins_losses_draws, [], now=NOW)

    assert overview.summary.closed_trade_count == 3
    assert (overview.summary.wins, overview.summary.losses, overview.summary.draws) == (1, 1, 1)
    assert overview.summary.win_rate == Decimal("50")
    assert overview.summary.realized_return_pct == Decimal("1.25")
    assert [point.cumulative_realized_pnl for point in overview.equity_curve] == [10_000, 5_000, 5_000]


def test_현재_비용모델_기반_실현손익은_완전대사가_아닌_estimated로_표시한다():
    overview = build_dashboard_overview(PERIOD, [_closed(realized_pnl=10_000)], [], now=NOW)

    assert overview.summary.realized_pnl == 10_000
    assert overview.summary.cost_basis == "estimated"


@pytest.mark.parametrize("mark_price, marked_at", [
    (None, None),
    (103_000, NOW - timedelta(minutes=11)),
])
def test_mark가_없거나_10분을_초과해_stale인_열린_포지션은_평가손익을_만들지_않는다(
        mark_price, marked_at):
    overview = build_dashboard_overview(
        PERIOD, [], [_open(mark_price=mark_price, marked_at=marked_at)], now=NOW)

    position = overview.positions[0]
    assert position.unrealized_pnl is None
    assert position.valuation_status == "unavailable"
    assert overview.summary.unrealized_pnl is None
    assert overview.summary.unrealized_pnl_status == "unavailable"
    assert overview.summary.total_pnl is None
    assert overview.summary.total_pnl_status == "unavailable"
    assert overview.freshness.latest_marked_at == marked_at


def test_유효한_mark와_확인불가_mark가_함께있으면_알려진_금액은_유지하고_총손익을_partial로_표시한다():
    overview = build_dashboard_overview(
        PERIOD,
        [_closed(realized_pnl=5_000)],
        [_open(position_id=10), _open(position_id=11, mark_price=None, marked_at=None)],
        now=NOW,
    )

    assert overview.summary.unrealized_pnl == 6_000
    assert overview.summary.unrealized_pnl_status == "partial"
    assert overview.summary.total_pnl == 11_000
    assert overview.summary.total_pnl_status == "partial"
    assert overview.freshness.mark_stale_after_seconds == 600


def test_미래시각_mark는_시계오염으로_간주해_평가와_최신기준시각에서_제외한다():
    future_mark = NOW + timedelta(seconds=1)

    overview = build_dashboard_overview(
        PERIOD, [], [_open(marked_at=future_mark)], now=NOW)

    assert overview.positions[0].unrealized_pnl is None
    assert overview.summary.unrealized_pnl is None
    assert overview.summary.total_pnl is None
    assert overview.freshness.latest_marked_at is None


def test_투자원금이_0이면_확정수익률은_임의의_0이_아니라_확인불가다():
    overview = build_dashboard_overview(
        PERIOD, [_closed(entry_price=0, quantity=0, realized_pnl=100)], [], now=NOW)

    assert overview.summary.realized_pnl == 100
    assert overview.summary.realized_return_pct is None


def test_정상적으로_거래와_포지션이_없으면_금액합계는_0이고_미확정으로_표시하지_않는다():
    overview = build_dashboard_overview(PERIOD, [], [], now=NOW)

    assert overview.summary.realized_pnl == 0
    assert overview.summary.unrealized_pnl == 0
    assert overview.summary.total_pnl == 0
    assert overview.summary.total_pnl_status == "complete"


def test_손상_행과_실현손익_없는_청산은_0으로_꾸미지_않고_제외_수를_보존한다():
    overview = build_dashboard_overview(
        PERIOD,
        [_closed(realized_pnl=None), _closed(position_id=2, realized_pnl=3_000)],
        [],
        now=NOW,
        corrupted_row_count=2,
    )

    assert overview.summary.closed_trade_count == 2
    assert overview.summary.realized_pnl == 3_000
    assert overview.summary.realized_pnl_status == "partial"
    assert overview.summary.incomplete_closed_trade_count == 1
    assert overview.summary.cost_basis == "unavailable"
    assert overview.warnings.corrupted_row_count == 2
    assert overview.warnings.incomplete_closed_trade_count == 1


def test_실현손익_없는_청산만_있을때_열린포지션이_없다는_0으로_총손익을_꾸미지_않는다():
    overview = build_dashboard_overview(
        PERIOD, [_closed(realized_pnl=None)], [], now=NOW)

    assert overview.summary.realized_pnl is None
    assert overview.summary.cost_basis == "unavailable"
    assert overview.summary.total_pnl is None
    assert overview.summary.total_pnl_status == "unavailable"
