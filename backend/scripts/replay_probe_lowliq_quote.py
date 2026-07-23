"""R1 잔여 실측 — 저유동 심볼의 ka10095 vs 분봉 '직전가 유지' 브리지 검증.

가정(스펙 §12, 트레이더 R2 Minor4): 무거래 분에는 분봉이 없고(결측),
리플레이는 last_at_or_before(직전가 유지)로 현재가를 답한다. 이 정책이
실서버 ka10095의 실시간 스냅샷 의미와 같은지 저유동 심볼로 확인한다 —
ka10095의 cur_prc가 "마지막 체결가"라면 두 세계는 일치한다.

실행 전제: 백엔드 정지(단일 토큰 — CLAUDE.md §5). G1 토큰 관용구.
"""

import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from app.adapters.kiwoom.auth import TokenManager
from app.adapters.kiwoom.client import KiwoomHttpClient
from app.adapters.kiwoom.rate_limiter import RateLimiter
from app.core.config import Settings

KST = ZoneInfo("Asia/Seoul")
MOCK_BASE = "https://mockapi.kiwoom.com"
DB_PATH = Path(__file__).resolve().parents[1] / "replay" / "data" / "minutes.sqlite"
LOWLIQ = "001067"   # JW중외제약2우B — 일평균 거래대금 ~1백만원
CONTROL = "005930"  # 유동 대조군


def last_minute_rows(symbol: str, limit: int = 3) -> list[tuple[str, str]]:
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT json_extract(row,'$.cntr_tm'), json_extract(row,'$.cur_prc') "
            "FROM minute_raw WHERE symbol=? ORDER BY seq LIMIT ?",
            (symbol, limit)).fetchall()
    finally:
        conn.close()
    return rows   # 수집 원문은 내림차순 저장 — seq 앞쪽이 최신


async def main() -> None:
    settings = Settings()
    if not settings.kiwoom_mock:
        print("SKIP: 모의서버 전용")
        return
    now = datetime.now(KST)
    print(f"[{now:%F %T}] ka10095 저유동 브리지 프로브")
    for symbol in (LOWLIQ, CONTROL):
        print(f"\n-- {symbol} 최신 분봉(수집본, 최신순 3):")
        for tm_, price in last_minute_rows(symbol):
            print(f"   {tm_} close={price}")

    async with httpx.AsyncClient(base_url=MOCK_BASE, timeout=15.0) as http:
        tm: TokenManager | None = None
        try:
            limiter = RateLimiter()
            tm = TokenManager(http,
                              settings.kiwoom_app_key.get_secret_value(),
                              settings.kiwoom_secret_key.get_secret_value(),
                              limiter=limiter)
            client = KiwoomHttpClient(settings, http=http, token_manager=tm,
                                      limiter=limiter)
            data, _, _ = await client.call(
                "stkinfo", "ka10095", {"stk_cd": f"{LOWLIQ}|{CONTROL}"})
            for row in data.get("atn_stk_infr", []):
                print(f"\n-- ka10095 {row.get('stk_cd')!r}: "
                      f"cur_prc={row.get('cur_prc')!r} "
                      f"cntr_tm={row.get('cntr_tm')!r} "
                      f"trde_qty={row.get('trde_qty')!r} "
                      f"sel_bid={row.get('sel_bid')!r} "
                      f"buy_bid={row.get('buy_bid')!r}")
        finally:
            if tm is not None:
                await tm.revoke()
                print("\n[토큰 revoke 완료]")


if __name__ == "__main__":
    asyncio.run(main())
