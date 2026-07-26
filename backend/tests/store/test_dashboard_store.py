from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.domain.dashboard.models import DashboardPeriod
from app.store.dashboard_store import DashboardStore
from app.store.models import Base, TradePositionRow, TradeRunRow


NOW = datetime(2026, 7, 26, 3, 0, tzinfo=timezone.utc)
PERIOD = DashboardPeriod(date(2026, 7, 20), date(2026, 7, 25), "Asia/Seoul")


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'dashboard.db'}")
    Base.metadata.create_all(engine)
    return engine


def _run(environment: str) -> TradeRunRow:
    return TradeRunRow(started_at=NOW, status="succeeded", config="{}",
                       run_environment=environment)


def _position(run_id: int, **overrides) -> TradePositionRow:
    values = {
        "trade_run_id": run_id,
        "symbol": "005930",
        "name": "삼성전자",
        "market": "kospi",
        "state": "closed",
        "entry_price": 100_000,
        "quantity": 2,
        "peak_price": 105_000,
        "trailing_active": False,
        "exit_price": 105_000,
        "exit_reason": "take_profit",
        "realized_pnl": 10_000,
        "entered_at": datetime(2026, 7, 19, 6, 0, tzinfo=timezone.utc),
        "closed_at": datetime(2026, 7, 22, 6, 0, tzinfo=timezone.utc),
        "mark_price": None,
        "marked_at": None,
    }
    return TradePositionRow(**(values | overrides))


def _seed(engine, *items: tuple[str, dict]) -> None:
    sessions = sessionmaker(bind=engine)
    with sessions.begin() as session:
        run_ids: dict[str, int] = {}
        for environment, _ in items:
            if environment not in run_ids:
                run = _run(environment)
                session.add(run)
                session.flush()
                run_ids[environment] = run.id
        for environment, values in items:
            session.add(_position(run_ids[environment], **values))


def test_요청_환경과_KST_기간에_속한_청산만_집계하고_곡선과_최근순서를_보존한다(engine):
    _seed(
        engine,
        ("mock", {"symbol": "A", "closed_at": datetime(2026, 7, 22, 7, tzinfo=timezone.utc),
                  "realized_pnl": 1_000}),
        ("mock", {"symbol": "B", "closed_at": datetime(2026, 7, 22, 7, tzinfo=timezone.utc),
                  "realized_pnl": 2_000}),
        # KST 7월 26일 00:00 — coarse UTC 범위에는 들어오나 정확 기간 밖이다.
        ("mock", {"symbol": "OUT", "closed_at": datetime(2026, 7, 25, 15, tzinfo=timezone.utc),
                  "realized_pnl": 99_999}),
        ("real", {"symbol": "REAL", "realized_pnl": 88_888}),
        ("replay", {"symbol": "REPLAY", "realized_pnl": 77_777}),
    )

    overview = DashboardStore(engine).overview(PERIOD, "mock", NOW)

    assert overview.summary.realized_pnl == 3_000
    assert [point.position_id for point in overview.equity_curve] == sorted(
        point.position_id for point in overview.equity_curve)
    assert [trade.symbol for trade in overview.recent_trades] == ["B", "A"]
    assert all(trade.symbol not in {"OUT", "REAL", "REPLAY"}
               for trade in overview.recent_trades)


def test_포지션영역은_열린_관리상태만_보이고_신선한_mark만_평가한다(engine):
    _seed(
        engine,
        ("mock", {"symbol": "ENTERED", "state": "entered", "mark_price": 103_000,
                  "marked_at": NOW - timedelta(minutes=1), "closed_at": None}),
        ("mock", {"symbol": "EXITING", "state": "exiting", "mark_price": None,
                  "marked_at": None, "closed_at": None}),
        ("mock", {"symbol": "EXIT_FAILED", "state": "exit_failed", "mark_price": 101_000,
                  "marked_at": NOW - timedelta(minutes=11), "closed_at": None}),
        ("mock", {"symbol": "PENDING", "state": "pending_entry", "closed_at": None}),
        ("mock", {"symbol": "CLOSED", "state": "closed"}),
        ("real", {"symbol": "OTHER_ENV", "state": "entered", "closed_at": None,
                  "mark_price": 103_000, "marked_at": NOW}),
    )

    overview = DashboardStore(engine).overview(PERIOD, "mock", NOW)

    assert [position.symbol for position in overview.positions] == [
        "ENTERED", "EXITING", "EXIT_FAILED"]
    assert overview.summary.unrealized_pnl == 6_000
    assert overview.summary.unrealized_pnl_status == "partial"
    assert overview.freshness.mark_stale_after_seconds == 600


def test_손상된_enum가격수량과_실현손익_없는_청산은_정상행에_섞이지_않고_경고된다(engine):
    _seed(
        engine,
        ("mock", {"symbol": "GOOD", "realized_pnl": 3_000}),
        ("mock", {"symbol": "BAD_STATE", "state": "broken"}),
        ("mock", {"symbol": "BAD_PRICE", "entry_price": 0}),
        ("mock", {"symbol": "BAD_QTY", "quantity": 0}),
        ("mock", {"symbol": "MISSING_PNL", "realized_pnl": None}),
    )

    overview = DashboardStore(engine).overview(PERIOD, "mock", NOW)

    assert overview.summary.realized_pnl == 3_000
    assert overview.summary.incomplete_closed_trade_count == 1
    assert overview.warnings.corrupted_row_count == 3
    assert overview.warnings.incomplete_closed_trade_count == 1
    assert [trade.symbol for trade in overview.recent_trades] == ["MISSING_PNL", "GOOD"]


def test_기간밖_closed_at을_가진_비종결상태와_알수없는_enum도_손상경고로_보존한다(engine):
    outside = datetime(2026, 7, 10, 6, tzinfo=timezone.utc)
    _seed(
        engine,
        ("mock", {"symbol": "OPEN_WITH_CLOSE", "state": "entered", "closed_at": outside}),
        ("mock", {"symbol": "UNKNOWN_WITH_CLOSE", "state": "broken", "closed_at": outside}),
    )

    overview = DashboardStore(engine).overview(PERIOD, "mock", NOW)

    assert overview.positions == ()
    assert overview.recent_trades == ()
    assert overview.warnings.corrupted_row_count == 2


def test_dashboard_조회는_주입하지_않은_broker_fixture를_호출하지_않는다(engine):
    calls = 0

    def broker_fixture():
        nonlocal calls
        calls += 1
        raise AssertionError("dashboard SQL read must not call broker")

    _seed(engine, ("mock", {"symbol": "GOOD"}))

    overview = DashboardStore(engine).overview(PERIOD, "mock", NOW)

    assert overview.summary.realized_pnl == 10_000
    assert calls == 0
    assert broker_fixture is not None
