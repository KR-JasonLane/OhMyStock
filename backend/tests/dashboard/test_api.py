"""관리매매 성과 dashboard HTTP 계약."""

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.domain.dashboard.models import (
    ClosedPosition,
    DashboardPeriod,
    OpenPosition,
    build_dashboard_overview,
)
from app.main import create_app


KST = ZoneInfo("Asia/Seoul")


class FakeDashboardStore:
    """HTTP 경계가 사용하는 읽기 포트의 관측 가능한 대역."""

    def __init__(self, error: Exception | None = None, *, naive_as_of=False,
                 recent_trade_count=2):
        self.calls: list[tuple[DashboardPeriod, str, datetime]] = []
        self.error = error
        self.naive_as_of = naive_as_of
        self.recent_trade_count = recent_trade_count

    def overview(self, period: DashboardPeriod, run_environment: str,
                 now: datetime):
        self.calls.append((period, run_environment, now))
        if self.error is not None:
            raise self.error
        closed_positions = [
                ClosedPosition(
                    position_id=1, symbol="005930", name="삼성전자",
                    entry_price=100_000, quantity=2, exit_price=105_000,
                    realized_pnl=10_000,
                    closed_at=datetime.combine(
                        period.start, datetime.min.time(), tzinfo=KST),
                    exit_reason="take_profit",
                ),
                ClosedPosition(
                    position_id=2, symbol="000660", name="SK하이닉스",
                    entry_price=100_000, quantity=1, exit_price=99_000,
                    realized_pnl=-1_000,
                    closed_at=datetime.combine(
                        period.end, datetime.max.time(), tzinfo=KST),
                    exit_reason="stop_loss",
                ),
            ]
        closed_positions.extend(
            ClosedPosition(
                position_id=100 + position_id, symbol=f"T{position_id:03}",
                name="최근거래", entry_price=100_000, quantity=1,
                exit_price=101_000, realized_pnl=1_000,
                closed_at=datetime.combine(
                    period.end, datetime.max.time(), tzinfo=KST),
                exit_reason="take_profit",
            )
            for position_id in range(3, self.recent_trade_count + 1)
        )
        overview = build_dashboard_overview(
            period,
            closed_positions,
            [
                OpenPosition(
                    position_id=3, symbol="035420", name="NAVER",
                    entry_price=100_000, quantity=1,
                    entered_at=now - timedelta(days=1), mark_price=101_000,
                    marked_at=now,
                ),
            ],
            now=now,
        )
        if self.naive_as_of:
            return replace(
                overview,
                freshness=replace(overview.freshness, as_of=now.replace(tzinfo=None)),
            )
        return overview


class ForbiddenWritePath:
    """조회 route가 운영 제어·거래 시작/정지에 손대면 즉시 실패시키는 spy."""

    def __init__(self):
        self.calls: list[str] = []

    def start(self):
        self.calls.append("start")
        raise AssertionError("dashboard GET must not start trading")

    async def request_stop_durable(self, *args, **kwargs):
        self.calls.append("request_stop_durable")
        raise AssertionError("dashboard GET must not stop trading")

    async def pause_scheduler(self, *args, **kwargs):
        self.calls.append("pause_scheduler")
        raise AssertionError("dashboard GET must not control operations")

    def current_task(self):
        return None


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        kiwoom_app_key="AK",
        kiwoom_secret_key="SK",
        kiwoom_mock=True,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'dashboard-api.db'}",
    )


@pytest.fixture
def client_and_store(tmp_path):
    app = create_app(_settings(tmp_path))
    store = FakeDashboardStore()
    with TestClient(app) as client:
        app.state.dashboard_store = store
        yield client, app, store


def test_기본_기간은_KST_오늘을_포함한_최근_30일이다(client_and_store):
    client, _, store = client_and_store

    response = client.get("/dashboard/overview")

    assert response.status_code == 200
    period, _, _ = store.calls[0]
    assert period.end == datetime.now(KST).date()
    assert period.start == period.end - timedelta(days=29)


def test_명시한_기간의_양끝을_store에_전달하고_영역별_JSON_계약을_반환한다(
        client_and_store):
    client, _, store = client_and_store

    response = client.get(
        "/dashboard/overview",
        params={"from": "2026-07-20", "to": "2026-07-25", "timezone": "Asia/Seoul"},
    )

    assert response.status_code == 200
    period, run_environment, _ = store.calls[0]
    assert period == DashboardPeriod(date(2026, 7, 20), date(2026, 7, 25),
                                    "Asia/Seoul")
    assert run_environment == "mock"
    body = response.json()
    assert set(body) == {
        "environment", "period", "summary", "equity_curve", "positions", "recent_trades",
        "freshness", "warnings",
    }
    assert body["environment"] == "mock"
    assert body["period"] == {
        "start": "2026-07-20", "end": "2026-07-25", "timezone": "Asia/Seoul",
    }
    assert len(body["recent_trades"]) == 2
    assert body["summary"]["closed_trade_count"] == 2
    assert isinstance(body["summary"]["realized_return_pct"], (int, float))
    assert body["summary"]["realized_return_pct"] == float(Decimal("3"))
    timestamps = [
        body["freshness"]["as_of"],
        body["freshness"]["latest_marked_at"],
        *(point["closed_at"] for point in body["equity_curve"]),
        *(position["entered_at"] for position in body["positions"]),
        *(position["marked_at"] for position in body["positions"]),
        *(trade["closed_at"] for trade in body["recent_trades"]),
    ]
    assert all(datetime.fromisoformat(value).utcoffset() is not None
               for value in timestamps)


@pytest.mark.parametrize("expected", ["mock", "real", "replay"])
def test_응답은_현재_실행환경을_정확한_transport_metadata로_반환한다(
        client_and_store, expected):
    client, app, _ = client_and_store
    app.state.settings = SimpleNamespace(run_environment=expected)

    response = client.get("/dashboard/overview")

    assert response.status_code == 200
    assert response.json()["environment"] == expected


@pytest.mark.parametrize("params", [
    {"from": "2026-07-26", "to": "2026-07-25"},
    {"from": "2025-07-24", "to": "2026-07-25"},
    {"from": "not-a-date", "to": "2026-07-25"},
])
def test_유효하지_않은_기간은_422다(client_and_store, params):
    client, _, _ = client_and_store

    response = client.get("/dashboard/overview", params=params)

    assert response.status_code == 422


@pytest.mark.parametrize("value", ["UTC", "Asia/Tokyo"])
def test_Asia_Seoul_외_timezone은_422다(client_and_store, value):
    client, _, _ = client_and_store

    response = client.get("/dashboard/overview", params={"timezone": value})

    assert response.status_code == 422


def test_store_실패는_내부_문자열_없이_안정된_503_code로_변환한다(tmp_path):
    app = create_app(_settings(tmp_path))
    secret = "token=private account_no=1234 app_key=AK resp_body=raw"
    with TestClient(app) as client:
        app.state.dashboard_store = FakeDashboardStore(RuntimeError(secret))
        response = client.get("/dashboard/overview")

    assert response.status_code == 503
    assert response.json() == {"code": "dashboard_unavailable"}
    assert secret not in response.text
    for forbidden in ("resp_body", "token", "account_no", "app_key"):
        assert forbidden not in response.text


def test_offset없는_datetime은_응답에_노출하지_않고_503으로_격리한다(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        app.state.dashboard_store = FakeDashboardStore(naive_as_of=True)
        response = client.get("/dashboard/overview")

    assert response.status_code == 503
    assert response.json() == {"code": "dashboard_unavailable"}


def test_API는_store가_초과_반환해도_최근거래를_100건만_직렬화한다(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        app.state.dashboard_store = FakeDashboardStore(recent_trade_count=101)
        response = client.get("/dashboard/overview")

    assert response.status_code == 200
    assert len(response.json()["recent_trades"]) == 100


def test_dashboard_GET은_broker와_운영_제어_및_거래_경로를_호출하지_않는다(
        client_and_store, monkeypatch):
    client, app, _ = client_and_store
    broker_calls: list[str] = []

    async def forbidden_broker(*args, **kwargs):
        broker_calls.append("get_deposit")
        raise AssertionError("dashboard GET must not call broker")

    monkeypatch.setattr(app.state.broker, "get_deposit", forbidden_broker)
    operations = ForbiddenWritePath()
    trading = ForbiddenWritePath()
    app.state.operations_control = operations
    app.state.trading = trading

    response = client.get("/dashboard/overview")

    assert response.status_code == 200
    assert broker_calls == []
    assert operations.calls == []
    assert trading.calls == []
