"""/trade API 계약 — 인증 스코프(결정 #33)·3자 배타(§8-1)·상태 노출(§6-7)·
비활성 하드 게이트(TRADE_* 미설정 → 503)."""

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.trade import router
from app.core.background_service import StopMode
from app.core.config import Settings
from app.domain.trading.models import PositionState, TradePosition
from app.domain.trading.service import TradingProgress

T0 = datetime(2026, 7, 22, 0, 10, tzinfo=timezone.utc)


class FakeTrading:
    def __init__(self, running=False):
        self._running = running
        self.stop_mode = None

    def is_running(self):
        return self._running

    def start(self):
        return None if self._running else object()

    def request_stop(self, mode):
        self.stop_mode = mode

    def progress(self):
        return TradingProgress(run_id=7, status="running" if self._running
                               else "succeeded",
                               positions_count=1, warnings=("w1",),
                               daily_order_count=3, daily_order_krw=1_000_000,
                               kill_switch=None)

    def started_at_iso(self):
        return T0.isoformat()

    def finished_at_iso(self):
        return None


class FakeRunning:
    def __init__(self, running=False):
        self._running = running

    def is_running(self):
        return self._running


class FakeTradingStore:
    def open_positions(self, run_environment=None):
        pos = TradePosition(symbol="005930", name="삼성전자", market="kospi",
                            state=PositionState.ENTERED, entry_price=100_000,
                            quantity=9, peak_price=101_000,
                            trailing_active=True, entered_at=T0)
        return [(1, pos)], [4]


def make_client(trading=..., collection=None, scoring=None,
                write_token=None, trade_token=None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.trading = FakeTrading() if trading is ... else trading
    app.state.collection = collection or FakeRunning()
    app.state.scoring = scoring or FakeRunning()
    app.state.trading_store = FakeTradingStore()
    app.state.settings = Settings(
        _env_file=None, kiwoom_app_key="AK", kiwoom_secret_key="SK",
        database_url="sqlite+pysqlite:///:memory:",
        api_write_token=write_token, api_trade_token=trade_token)
    return TestClient(app)


def test_start_202():
    resp = make_client().post("/trade/start")
    assert resp.status_code == 202 and resp.json() == {"started": True}


def test_start_중복이면_409():
    resp = make_client(trading=FakeTrading(running=True)).post("/trade/start")
    assert resp.status_code == 409


def test_start_수집_실행중이면_409():
    resp = make_client(collection=FakeRunning(True)).post("/trade/start")
    assert resp.status_code == 409
    assert "collection" in resp.json()["detail"]


def test_start_스코어링_실행중이면_409():
    resp = make_client(scoring=FakeRunning(True)).post("/trade/start")
    assert resp.status_code == 409


def test_한도_미설정이면_503():
    # §8-1 하드 게이트 — TRADE_* 미설정 시 엔진 자체가 조립되지 않는다
    client = make_client(trading=None)
    assert client.post("/trade/start").status_code == 503
    assert client.post("/trade/stop").status_code == 503
    assert client.get("/trade/status").json()["status"] == "disabled"


def test_stop_기본은_신규진입_정지():
    trading = FakeTrading(running=True)
    resp = make_client(trading=trading).post("/trade/stop")
    assert resp.status_code == 200
    assert trading.stop_mode is StopMode.STOP_NEW_ENTRIES


def test_stop_전량청산_모드():
    trading = FakeTrading(running=True)
    resp = make_client(trading=trading).post(
        "/trade/stop", json={"mode": "liquidate_all"})
    assert resp.json() == {"stopping": True, "mode": "liquidate_all"}
    assert trading.stop_mode is StopMode.LIQUIDATE_ALL


def test_stop_미실행이면_409_미지모드는_422():
    assert make_client().post("/trade/stop").status_code == 409
    resp = make_client(trading=FakeTrading(running=True)).post(
        "/trade/stop", json={"mode": "sell_everything_now"})
    assert resp.status_code == 422


def test_status_계약():
    body = make_client(trading=FakeTrading(running=True)).get(
        "/trade/status").json()
    assert body["run_id"] == 7 and body["status"] == "running"
    assert body["warnings"] == ["w1"]
    assert body["daily_order_count"] == 3
    assert body["started_at"] == T0.isoformat()


def test_positions_계약():
    body = make_client().get("/trade/positions").json()
    assert body["corrupted_rows"] == [4]
    [row] = body["positions"]
    assert row["symbol"] == "005930" and row["state"] == "entered"
    assert row["trailing_active"] is True


# ── 인증 스코프(결정 #33) ───────────────────────────────────────────────

def test_trade_토큰_설정시_미제시는_401():
    client = make_client(trade_token="TR")
    assert client.post("/trade/start").status_code == 401
    assert client.post("/trade/start",
                       headers={"X-API-Key": "WRONG"}).status_code == 401
    assert client.post("/trade/start",
                       headers={"X-API-Key": "TR"}).status_code == 202


def test_trade_토큰이_있으면_write_토큰으로는_거부():
    # 스코프 분리 — 쓰기 토큰이 주문 권한으로 승격되면 안 된다
    client = make_client(write_token="WR", trade_token="TR")
    assert client.post("/trade/start",
                       headers={"X-API-Key": "WR"}).status_code == 401
    assert client.post("/trade/start",
                       headers={"X-API-Key": "TR"}).status_code == 202


def test_trade_토큰_미설정시_write_토큰_폴백():
    client = make_client(write_token="WR")
    assert client.post("/trade/start").status_code == 401
    assert client.post("/trade/start",
                       headers={"X-API-Key": "WR"}).status_code == 202


def test_조회는_토큰_없이_개방():
    client = make_client(trade_token="TR")
    assert client.get("/trade/status").status_code == 200
    assert client.get("/trade/positions").status_code == 200
