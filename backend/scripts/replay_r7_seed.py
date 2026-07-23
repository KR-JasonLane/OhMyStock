"""R7 예행용 리플레이 DB 시드(스펙 §10-2 — 검증 하네스, 프로덕션 무접촉).

별도 DATABASE_URL(리플레이 전용 — §4-1 기동 게이트가 프로덕션 DB 공유를
거부한다)에 진입 검증에 필요한 최소 데이터를 만든다:
- instruments/candles: **분봉 실측(minutes.sqlite)을 일봉으로 집계** —
  가짜 값이 아니라 실측 유래(신호가·유동성·갭 가드가 재생 세계와 정합).
- score_runs(reference=앵커 직전 거래일) + analysis_runs/verdict(검증용
  주입 — model='MANUAL-R7'로 명시, 실분석 아님).

사용: DATABASE_URL=sqlite+pysqlite:///... 로 alembic upgrade head 후 실행.
"""

import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.store.models import (AnalysisRunRow, AnalysisVerdictRow, CandleRow,
                              InstrumentRow, ScoreRunRow)

MINUTES = Path(__file__).resolve().parents[1] / "replay" / "data" / "minutes.sqlite"
# 시나리오 파라미터(env 오버라이드 — run별 재사용):
SYMBOL = os.environ.get("R7_SYMBOL", "035760")
NAME = os.environ.get("R7_NAME", SYMBOL)
MARKET = os.environ.get("R7_MARKET", "kosdaq")
SIGNAL_DATE = date.fromisoformat(
    os.environ.get("R7_SIGNAL_DATE", "2026-06-24"))  # 앵커 직전 거래일
DAYS = 25


def daily_from_minutes() -> list[dict]:
    conn = sqlite3.connect(MINUTES)
    try:
        rows = conn.execute(
            "SELECT json_extract(row,'$.cntr_tm'), json_extract(row,'$.open_pric'), "
            "json_extract(row,'$.high_pric'), json_extract(row,'$.low_pric'), "
            "json_extract(row,'$.cur_prc'), json_extract(row,'$.trde_qty') "
            "FROM minute_raw WHERE symbol=?", (SYMBOL,)).fetchall()
    finally:
        conn.close()
    days: dict[str, list] = {}
    for tm, o, h, low, c, v in rows:
        days.setdefault(tm[:8], []).append((tm, abs(int(o)), abs(int(h)),
                                            abs(int(low)), abs(int(c)), int(v)))
    out = []
    for day in sorted(days):
        if day > SIGNAL_DATE.strftime("%Y%m%d"):
            continue
        mins = sorted(days[day])
        out.append({
            "date": datetime.strptime(day, "%Y%m%d").date(),
            "open": mins[0][1],
            "high": max(m[2] for m in mins),
            "low": min(m[3] for m in mins),
            "close": mins[-1][4],
            "volume": sum(m[5] for m in mins),
        })
    return out[-DAYS:]


def main() -> None:
    url = os.environ["DATABASE_URL"]
    assert "replay" in url, f"리플레이 전용 DB URL이어야 합니다: {url}"
    engine = create_engine(url)
    now = datetime.now(timezone.utc)
    candles = daily_from_minutes()
    assert candles[-1]["date"] == SIGNAL_DATE, candles[-1]
    with sessionmaker(bind=engine).begin() as session:
        session.add(InstrumentRow(symbol=SYMBOL, name=NAME, market=MARKET,
                                  instrument_type="A", is_active=True,
                                  updated_at=now, state="증거금40%",
                                  audit_info="정상"))
        for c in candles:
            session.add(CandleRow(symbol=SYMBOL, **c))
        score = ScoreRunRow(started_at=now, finished_at=now,
                            status="succeeded", reference_date=SIGNAL_DATE,
                            universe_count=1, config="{}")
        session.add(score)
        session.flush()
        run = AnalysisRunRow(started_at=now, finished_at=now,
                             status="succeeded", score_run_id=score.id,
                             model="MANUAL-R7", prompt_hash="r7-inject",
                             config='{"note":"R7 예행용 주입"}',
                             regime="neutral",
                             market_summary="[R7 예행 주입] 실분석 아님",
                             max_picks_advice=1, economist_fallback=False)
        session.add(run)
        session.flush()
        session.add(AnalysisVerdictRow(run_id=run.id, symbol=SYMBOL,
                                       verdict="approve", confidence=0.9,
                                       reasons='["R7 예행 주입"]',
                                       risk_flags='["검증용"]',
                                       picked=True, pick_rank=1))
    print(f"seeded: {SYMBOL} candles={len(candles)} "
          f"signal_close={candles[-1]['close']:,} "
          f"avg_value~={int(sum(c['close']*c['volume'] for c in candles[-20:])/20):,}")


if __name__ == "__main__":
    main()
