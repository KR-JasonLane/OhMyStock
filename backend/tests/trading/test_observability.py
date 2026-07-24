"""트레이딩 관측성(P6 Task 7c, 결정 #36) — 판정·방어선이 로그와 DB에
남는지 고정.

배경(2026-07-24 7b 실환경 관찰): 진입 재시도 판정이 warnings 리스트에만
쌓여 ① 로그에 0건이라 grep 재구성 불가, ② run 종료와 함께 완전 소실돼
"그날 왜 안 샀나"를 SQL로 물을 수 없었다. 결정 #36의 두 요구(상세 로그·
분석 친화 적재)가 트레이딩 진입 경로에서 동시에 깨져 있던 상태."""

import asyncio
import logging
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine

from app.domain.broker import (Balance, Deposit, MarketData, OrderAck, Quote)
from app.domain.trading.config import TradingConfig
from app.domain.trading.models import PositionState, TradePosition
from app.domain.trading.monitor import PositionMonitor
from app.domain.trading.service import TradingService
from app.store.models import Base
from app.store.trading_store import TradingStore

KST = timezone(timedelta(hours=9))
T0 = datetime(2026, 7, 24, 0, 10, tzinfo=timezone.utc)   # 09:10 KST

CFG = TradingConfig(max_single_order_krw=100_000_000, max_daily_orders=100,
                    max_daily_order_krw=500_000_000,
                    min_avg_trading_value_krw=0,
                    limit_order_timeout_sec=3.0, exit_limit_timeout_sec=3.0,
                    poll_interval_sec=1.0, quote_failure_threshold=2)


@pytest.fixture(autouse=True)
def _trading_loggers_enabled():
    """alembic 마이그레이션 테스트의 fileConfig(disable_existing_loggers
    기본 True)가 ini에 없는 기존 로거를 세션 내내 비활성화한다 — 실행
    순서에 따라 caplog가 아무것도 못 잡는 현상(test_api_security.py의
    동일 픽스처 참고). 이 모듈이 검증하는 두 로거만 재활성화."""
    for name in ("app.domain.trading.service", "app.domain.trading.monitor"):
        logging.getLogger(name).disabled = False


class _Cal:
    """첫 사이클만 장중 — 진입 판정 1회 수행 후 루프 정상 종료."""

    KST = KST

    def __init__(self, hours=None):
        self._hours = list(hours if hours is not None else [True, False])

    def is_trading_day(self, d) -> bool:
        return True

    def is_market_hours(self, now) -> bool:
        return self._hours.pop(0) if self._hours else False

    def held_business_days(self, entry_date, now) -> int:
        return 0


class _Broker:
    async def get_quotes(self, symbols):
        q = Quote(symbol="005930", name="삼성전자", price=100_000,
                  change_rate=Decimal("0"), volume=0)
        return [MarketData(quote=q, bid=99_900, ask=100_100)
                for _ in symbols]

    async def place_order(self, req):
        return OrderAck(order_no="N1", message="ok")

    async def cancel_order(self, order_no, symbol):
        return OrderAck(order_no="C1", message="cancelled")

    async def get_open_orders(self):
        return []

    async def get_balance(self):
        return Balance((), 0, 0)

    async def get_deposit(self):
        return Deposit(total=10_000_000, available=10_000_000)


def _md(price: int) -> MarketData:
    q = Quote(symbol="005930", name="삼성전자", price=price,
              change_rate=Decimal("0"), volume=0)
    return MarketData(quote=q, bid=price - 100, ask=price + 100)


def _pos(**kw) -> TradePosition:
    base = dict(symbol="005930", name="삼성전자", market="kospi",
                state=PositionState.ENTERED, entry_price=100_000,
                quantity=10, peak_price=100_000, trailing_active=False,
                entered_at=T0)
    base.update(kw)
    return TradePosition(**base)


# ── ① 판정 로그 (grep 재구성 가능) ──────────────────────────────────────

@pytest.mark.anyio
async def test_진입_판정이_로그로_남는다(tmp_path, caplog):
    """`trade decision:` 한 줄로 grep 가능해야 한다 — 재시도 사유가
    로그에 0건이던 실측 결함(7b)의 회귀."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'obs.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    broker = _Broker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,   # 분석 결과 없음
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0,
        run_environment="mock")
    logging.getLogger("app.domain.trading.service").propagate = True
    with caplog.at_level(logging.WARNING, logger="app.domain.trading.service"):
        await service.run()
    lines = [r.getMessage() for r in caplog.records]
    assert any("trade decision: no analysis result yet" in m for m in lines)


@pytest.mark.anyio
async def test_판정_로그는_중복되지_않는다(tmp_path, caplog):
    """dedup — 재시도 사이클마다 같은 줄이 쌓이면 grep이 무의미해진다."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'obs2.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    broker = _Broker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0,
        run_environment="mock")
    logging.getLogger("app.domain.trading.service").propagate = True
    with caplog.at_level(logging.WARNING, logger="app.domain.trading.service"):
        service._warn_once("dup message")
        service._warn_once("dup message")
    assert sum("dup message" in r.getMessage()
               for r in caplog.records) == 1


# ── ② warnings DB 영속 (SQL 재구성 가능) ───────────────────────────────

@pytest.mark.anyio
async def test_run_종료시_warnings가_DB에_영속된다(tmp_path):
    """`/trade/status` 메모리에만 있던 판정 사유를 trade_runs.warnings로
    (0012) — run 종료 후에도 SQL로 "왜 안 샀나"를 물을 수 있어야 한다."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'obs3.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    broker = _Broker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0,
        run_environment="mock")
    await service.run()
    import sqlite3
    conn = sqlite3.connect(tmp_path / 'obs3.db')
    (saved,) = conn.execute(
        "select warnings from trade_runs order by id desc limit 1").fetchone()
    conn.close()
    assert saved is not None
    assert "no analysis result yet" in saved


@pytest.mark.anyio
async def test_monitor_경고도_함께_영속된다(tmp_path):
    """트레이더 T7c Important — progress()는 두 출처(서비스+monitor)를
    합치는데 finish_run만 서비스 것만 저장하면, 방어선 신뢰성 경고
    (persist:/quote: 실패)가 run 종료 시 조용히 소실된다."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'obs5.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    broker = _Broker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0,
        run_environment="mock")
    service.start()                      # _on_accepted가 monitor 생성
    # monitor.warnings는 내부 dict의 values() 스냅샷을 반환하는 property —
    # 상시 경고를 흉내내려면 내부 dict에 넣는다(실코드 경로와 동일).
    service._monitor._warnings["quote:005930"] = (
        "005930: quote poll failing (3 consecutive)")
    await service.current_task()
    import sqlite3
    conn = sqlite3.connect(tmp_path / 'obs5.db')
    (saved,) = conn.execute(
        "select warnings from trade_runs order by id desc limit 1").fetchone()
    conn.close()
    assert "quote poll failing" in saved          # monitor 출처 포함
    assert "no analysis result yet" in saved      # 서비스 출처도 유지


@pytest.mark.anyio
async def test_경고가_없으면_None이_영속된다(tmp_path):
    """`_collected_warnings()`의 빈 분기 직접 검증(개발자 T7c 델타 —
    절단 테스트가 이 분기를 대신하지 못한다). 장 마감 후 재기동처럼
    진입 판정 자체가 없는 자연스러운 시나리오."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'obs7.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    broker = _Broker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(hours=[False]),   # 첫 틱부터 장 마감
        lambda: None, sleep=lambda _s: asyncio.sleep(0), now=lambda: T0,
        run_environment="mock")
    await service.run()
    assert service._collected_warnings() is None
    import sqlite3
    conn = sqlite3.connect(tmp_path / 'obs7.db')
    (saved,) = conn.execute(
        "select warnings from trade_runs order by id desc limit 1").fetchone()
    conn.close()
    assert saved is None


def test_절단시_잘린_건수를_남긴다(tmp_path):
    """상한 200 초과 시 "전부 다"로 오인하지 않도록 마커(트레이더 Minor)."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'obs6.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    broker = _Broker()
    service = TradingService(
        broker, broker, store, CFG, _Cal(), lambda: None,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0,
        run_environment="mock")
    service._warnings = [f"w{i}" for i in range(250)]
    body = service._collected_warnings()
    assert body.startswith("[50 earlier warnings truncated]")
    assert body.endswith("w249")          # 최신 우선 보존


def test_warnings_없으면_None(tmp_path):
    """빈 문자열이 아니라 NULL — 집계 쿼리에서 "경고 있음"을 IS NOT NULL로
    물을 수 있게."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'obs4.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine, now=lambda: T0)
    run_id = store.create_run("{}", "mock")
    store.finish_run(run_id, "succeeded")
    import sqlite3
    conn = sqlite3.connect(tmp_path / 'obs4.db')
    (saved,) = conn.execute(
        "select warnings from trade_runs where id=?", (run_id,)).fetchone()
    conn.close()
    assert saved is None


# ── ③ 방어선 상태 전이 로그 ────────────────────────────────────────────

@pytest.mark.anyio
async def test_트레일링_활성화가_로그로_남는다(caplog):
    """"손절/트레일링이 왜 그 가격에 발동했나"의 사후 재구성 재료 —
    활성화(되돌릴 수 없는 래치)는 WARNING."""
    persisted = []
    monitor = PositionMonitor(
        _Broker(), CFG, _Cal(), lambda amount, side: None,
        persist_position=persisted.append,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0)
    logging.getLogger("app.domain.trading.monitor").propagate = True
    with caplog.at_level(logging.INFO, logger="app.domain.trading.monitor"):
        # +6% — trailing_activate_pct(5%) 초과 → 래치
        monitor._evaluate(_pos(), _md(106_000), T0)
    lines = [r.getMessage() for r in caplog.records]
    assert any("defense trailing ACTIVATED" in m and "005930" in m
               for m in lines)


@pytest.mark.anyio
async def test_peak_갱신이_로그로_남는다(caplog):
    persisted = []
    monitor = PositionMonitor(
        _Broker(), CFG, _Cal(), lambda amount, side: None,
        persist_position=persisted.append,
        sleep=lambda _s: asyncio.sleep(0), now=lambda: T0)
    logging.getLogger("app.domain.trading.monitor").propagate = True
    with caplog.at_level(logging.INFO, logger="app.domain.trading.monitor"):
        monitor._evaluate(_pos(), _md(102_000), T0)   # +2% — 래치 전 peak만
    lines = [r.getMessage() for r in caplog.records]
    assert any("defense peak updated" in m for m in lines)
    assert not any("ACTIVATED" in m for m in lines)
