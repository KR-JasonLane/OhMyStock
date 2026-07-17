"""실제 키움 모의서버 스모크. 실행: uv run pytest -m live -v
.env에 실제 발급 키 필요. KIWOOM_MOCK=true인 경우에만 실행된다.

각 테스트가 독립된 KiwoomHttpClient(→ 독립된 TokenManager)를 생성/폐기하다 보니,
전체 스위트를 한 번에 실행하면 /oauth2/token 발급이 짧은 시간 안에 여러 번 몰려
모의서버의 발급 레이트리밋(429)에 걸리는 경우가 실측으로 확인됐다 (TR 호출과 달리
토큰 발급에는 재시도 백오프가 없다 — auth.py는 Task 3 소유라 이 스모크에서는 건드리지
않고, 테스트 쪽에서 짧게 재시도한다). 운영 경로(Task 8)는 앱 생애주기 동안 클라이언트
하나만 공유하므로 이 충돌이 발생하지 않는다 — 순수 테스트 구조 아티팩트."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TypeVar

import httpx
import pytest

from app.adapters.kiwoom.auth import KST, TokenManager
from app.adapters.kiwoom.broker import KiwoomBroker
from app.adapters.kiwoom.client import KiwoomHttpClient
from app.domain.errors import RateLimitError
from app.core.config import Settings

pytestmark = pytest.mark.live

MOCK_BASE = "https://mockapi.kiwoom.com"

T = TypeVar("T")


async def _retry_on_token_rate_limit(fn: Callable[[], Awaitable[T]]) -> T:
    """토큰 발급 429 충돌 시 짧게 대기 후 재시도한다 (최대 3회)."""
    for delay in (2.0, 4.0, None):
        try:
            return await fn()
        except RateLimitError:
            if delay is None:
                raise
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


@pytest.fixture
def settings() -> Settings:
    s = Settings()  # .env에서 로드
    if not s.kiwoom_mock:
        pytest.skip("라이브 스모크는 모의서버(KIWOOM_MOCK=true)에서만 실행한다")
    return s


@pytest.mark.anyio
async def test_live_토큰_발급과_폐기(settings):
    async with httpx.AsyncClient(base_url=MOCK_BASE, timeout=10) as http:
        tm = TokenManager(http, settings.kiwoom_app_key.get_secret_value(),
                           settings.kiwoom_secret_key.get_secret_value())
        token = await tm.get_token()
        assert token  # 값 자체는 출력하지 않는다
        await tm.revoke()


@pytest.mark.anyio
async def test_live_삼성전자_현재가(settings):
    b = KiwoomBroker(KiwoomHttpClient(settings))
    try:
        q = await _retry_on_token_rate_limit(lambda: b.get_quote("005930"))
        assert q.name and q.price > 0
        print(f"[live] 005930 {q.name} price={q.price} rate={q.change_rate}")
    finally:
        await b.aclose()


@pytest.mark.anyio
async def test_live_삼성전자_일봉_5개(settings):
    b = KiwoomBroker(KiwoomHttpClient(settings))
    try:
        candles = await _retry_on_token_rate_limit(
            lambda: b.get_daily_candles("005930", count=5))
        assert len(candles) == 5
        assert candles[0].date < candles[-1].date  # 과거→최신
        assert all(c.high >= c.low > 0 for c in candles)
    finally:
        await b.aclose()


@pytest.mark.anyio
async def test_live_예수금과_잔고(settings):
    b = KiwoomBroker(KiwoomHttpClient(settings))
    try:
        d = await _retry_on_token_rate_limit(lambda: b.get_deposit())
        assert d.total >= 0 and d.available >= 0
        bal = await _retry_on_token_rate_limit(lambda: b.get_balance())
        assert bal.total_eval >= 0
        print(f"[live] deposit={d.total} positions={len(bal.positions)}")
        if bal.positions:
            for p in bal.positions:
                print(f"[live] position symbol={p.symbol} avg_price(parsed)={p.avg_price}")
        else:
            print("[live] no positions in mock account - avg_price 정수원단위 "
                  "실측은 보류 (포지션 보유 시 재검증 필요)")
    finally:
        await b.aclose()


@pytest.mark.anyio
async def test_live_종목리스트_코스피(settings):
    b = KiwoomBroker(KiwoomHttpClient(settings))
    try:
        items = await _retry_on_token_rate_limit(lambda: b.list_instruments("kospi"))
        assert len(items) > 100
        print(f"[live] kospi instruments={len(items)} sample={items[0].symbol} "
              f"instrument_type={items[0].instrument_type!r}")
    finally:
        await b.aclose()


@pytest.mark.anyio
async def test_live_업종코드와_구성종목(settings):
    # stex_tp="1"이 코스피뿐 아니라 코스닥 업종 조회에서도 유효한지 실측한다 —
    # 코스피 업종만 검증하면 stex_tp가 시장별로 달라야 할 가능성을 놓친다.
    b = KiwoomBroker(KiwoomHttpClient(settings))
    try:
        sectors = await _retry_on_token_rate_limit(lambda: b.list_sectors())
        assert sectors
        kospi_sector = next(s for s in sectors if s.market == "kospi")
        kosdaq_sector = next(s for s in sectors if s.market == "kosdaq")

        kospi_members = await _retry_on_token_rate_limit(
            lambda: b.list_sector_members(kospi_sector.code, kospi_sector.market))
        kosdaq_members = await _retry_on_token_rate_limit(
            lambda: b.list_sector_members(kosdaq_sector.code, kosdaq_sector.market))

        print(f"[live] sectors={len(sectors)} "
              f"kospi={kospi_sector.code} members={len(kospi_members)} "
              f"kosdaq={kosdaq_sector.code} members={len(kosdaq_members)}")
        assert isinstance(kospi_members, list) and isinstance(kosdaq_members, list)
    finally:
        await b.aclose()


@pytest.mark.anyio
async def test_live_잔고_원본응답_avg_price_실측(settings):
    """broker.py의 파싱을 우회해 kt00018 원본 pur_pric 문자열을 실측하고,
    Position.avg_price의 파싱 결과와 나란히 출력한다 (원 단위 정수 여부 검증)."""
    client = KiwoomHttpClient(settings)
    try:
        data, _, _ = await _retry_on_token_rate_limit(lambda: client.call(
            "acnt", "kt00018", {"qry_tp": "1", "dmst_stex_tp": "KRX"}))
        rows = data.get("acnt_evlt_remn_indv_tot") or []
        if not rows:
            print("[live] no positions in mock account - avg_price 정수원단위 "
                  "실측은 보류 (포지션 보유 시 재검증 필요)")
            return
        for row in rows:
            raw = row.get("pur_pric")
            parsed = abs(int(raw)) if raw and raw.strip() else 0
            print(f"[live] symbol={row.get('stk_cd')} raw_pur_pric={raw!r} "
                  f"avg_price(parsed)={parsed}")
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_live_일봉_원본응답은_최신부터다(settings):
    """broker.py의 정렬 로직을 우회해 키움 원본 응답 순서를 실측한다 —
    rows[:count] 절단이 실제로 최신 봉들을 취하는지 증명한다."""
    client = KiwoomHttpClient(settings)
    try:
        body = {
            "stk_cd": "005930",
            "base_dt": datetime.now(KST).strftime("%Y%m%d"),
            "upd_stkpc_tp": "1",
        }
        data, _, _ = await _retry_on_token_rate_limit(
            lambda: client.call("chart", "ka10081", body))
        rows = data["stk_dt_pole_chart_qry"]
        assert rows[0]["dt"] > rows[-1]["dt"]  # 내림차순 = 최신→과거
        print(f"[live] raw dt order (first 3): {[r['dt'] for r in rows[:3]]}")
    finally:
        await client.aclose()
