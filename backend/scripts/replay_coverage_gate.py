"""임계 크로스 커버리지 게이트(리플레이 스펙 §6 ①~③).

두 모드(트레이더 R2 #1/broker-api R1 #2 — 전체 이력 통계는 특정 앵커
윈도우의 커버리지를 보장하지 않는다):
  전체 모드(기본): 앵커 **후보 탐색용 참고 자료** — 심볼별 첫 임계 크로스
    시각을 함께 출력해 사람이 표만 보고 앵커를 찍지 않게 한다.
  윈도우 모드(--anchor YYYY-MM-DD --days N): **R7 앵커 확정의 필수 근거** —
    그 재생 구간 안에서의 커버리지만 판정한다(스펙 §6 "각 심볼·재생 구간").

임계값은 app.domain.trading.config.TradingConfig에서 임포트(트레이더 R2
Minor5 — 하드코딩 사본은 튜닝 시 조용히 stale. scripts/는 replay 임포트
격리 대상이 아니다). 실행:
  cd backend && PYTHONPATH=. uv run python scripts/replay_coverage_gate.py \
      [--anchor 2026-06-25 --days 5] [--db replay/data/minutes.sqlite]

부가 산출(broker-api R1 #1 — 결측 분 정책의 실측 근거 기록):
  심볼별 정규장(09:00~15:20) 내 결측 분 개수 — "무거래 분은 봉이 없다"의
  증거. 결과를 .superpowers/sdd/replay-ka10080-coverage.txt로 보존할 것."""

import argparse
from datetime import date, timedelta

from app.core.market_calendar import is_trading_day
from app.domain.trading.config import TradingConfig
from replay.minute_store import MinuteCandle, MinuteStore

# 임계값 원천 — 버그 봉쇄 한도 4종은 게이트와 무관한 필수 인자라 더미
_CFG = TradingConfig(max_single_order_krw=1, max_daily_orders=1,
                     max_daily_order_krw=1, min_avg_trading_value_krw=0)

# 수집 목표 심볼(카테고리 실종 감지 — 트레이더 R2 #2: 012510처럼 로드에서
# 탈락한 심볼이 대표하던 카테고리가 조용히 사라지는 것 방지)
_TARGET_SYMBOLS = {
    "005930": "코스피 대형", "000660": "코스피 대형", "005380": "코스피 대형",
    "051910": "코스피 대형", "247540": "코스닥", "196170": "코스닥",
    "035760": "코스닥", "069500": "ETF", "371460": "ETF",
    "005935": "우선주", "012510": "저유동/결함응답", "028300": "코스닥",
}


def _window_end(anchor: date, days: int) -> date:
    d, count = anchor, 0
    while count < days:
        if is_trading_day(d):
            count += 1
        d += timedelta(days=1)
    return d


def _stats(series: list[MinuteCandle]) -> tuple[float, float, str, str, int]:
    """(maxDD%, maxRU%, 첫 손절크로스 시각, 첫 익절크로스 시각, 정규장 결측 분)."""
    peak = trough = series[0].close
    max_dd = max_ru = 0.0
    first_dd = first_ru = "-"
    gaps = 0
    prev_ts = None
    for candle in series:
        peak = max(peak, candle.close)
        trough = min(trough, candle.close)
        dd = (peak - candle.close) / peak * 100
        ru = (candle.close - trough) / trough * 100
        max_dd = max(max_dd, dd)
        if first_dd == "-" and dd >= _CFG.stop_loss_pct:
            first_dd = candle.ts.strftime("%m-%d %H:%M")
        max_ru = max(max_ru, ru)
        if first_ru == "-" and ru >= _CFG.take_profit_pct:
            first_ru = candle.ts.strftime("%m-%d %H:%M")
        # 정규장(09:00~15:20) 연속성 — 결측 분 실측(동시호가 구간 제외)
        if (prev_ts is not None and candle.ts.date() == prev_ts.date()
                and prev_ts.time().hour * 60 + prev_ts.time().minute >= 540
                and candle.ts.time().hour * 60 + candle.ts.time().minute <= 920):
            delta = int((candle.ts - prev_ts).total_seconds() // 60) - 1
            if delta > 0:
                gaps += delta
        prev_ts = candle.ts
    return max_dd, max_ru, first_dd, first_ru, gaps


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="replay/data/minutes.sqlite")
    parser.add_argument("--anchor", help="재생 앵커일 YYYY-MM-DD (윈도우 모드)")
    parser.add_argument("--days", type=int, default=5,
                        help="앵커부터 재생 거래일 수 (윈도우 모드)")
    args = parser.parse_args()

    since = None
    window = None
    if args.anchor:
        anchor = date.fromisoformat(args.anchor)
        window = (anchor, _window_end(anchor, args.days))
        from datetime import datetime
        since = datetime(anchor.year, anchor.month, anchor.day)
    store = MinuteStore.load(args.db, since=since)

    mode = (f"윈도우 모드 anchor={window[0]} 거래일={args.days} "
            f"(~{window[1]} 미포함)" if window else
            "전체 모드 — ⚠️ 앵커 후보 탐색용 참고. R7 앵커 확정은 반드시 "
            "--anchor 윈도우 모드 결과를 근거로(스펙 §6 '각 심볼·재생 구간')")
    print(f"[{mode}]")
    print(f"loaded symbols={len(store.symbols)} skipped_rows={store.skipped}")

    missing = set(_TARGET_SYMBOLS) - set(store.symbols)
    for symbol in sorted(missing):
        print(f"⚠️ 목표 심볼 미로드: {symbol} — 카테고리 "
              f"'{_TARGET_SYMBOLS[symbol]}' 커버리지 공백(대체 심볼 필요 여부 "
              "판단할 것)")

    print(f"{'symbol':8} {'days':>5} {'maxDD%':>7} {'maxRU%':>7} "
          f"{'첫손절크로스':>12} {'첫익절크로스':>12} {'결측분':>6}")
    any_dd = any_ru8 = any_ru10 = False
    for symbol in store.symbols:
        series = [c for c in store.candles(symbol)
                  if window is None or window[0] <= c.ts.date() < window[1]]
        if not series:
            print(f"{symbol:8} {'(윈도우 내 데이터 없음)':>20}")
            continue
        days = len({c.ts.date() for c in series})
        max_dd, max_ru, first_dd, first_ru, gaps = _stats(series)
        any_dd |= max_dd >= _CFG.stop_loss_pct
        any_ru8 |= max_ru >= _CFG.trailing_widen_until_pct
        any_ru10 |= max_ru >= _CFG.take_profit_pct
        print(f"{symbol:8} {days:5d} {max_dd:7.2f} {max_ru:7.2f} "
              f"{first_dd:>12} {first_ru:>12} {gaps:6d}")
    print("\n[게이트 판정]"
          + ("" if window else " ⚠️ 전체 이력 기준 — R7 근거로 사용 금지"))
    print(f"  손절(-{_CFG.stop_loss_pct}%): {'커버' if any_dd else '미검증!'}")
    print(f"  트레일링 안착(+{_CFG.trailing_widen_until_pct}%): "
          f"{'커버' if any_ru8 else '미검증!'}")
    print(f"  익절(+{_CFG.take_profit_pct}%): "
          f"{'커버' if any_ru10 else '미검증!'}")
    print(f"  보유기간(기본 {_CFG.max_holding_days}일): 윈도우 일수 참조 — "
          "미달 시 config 단축(max_holding_days)이 1차 수단(스펙 §6)")
    print("  ※ 결측분>0 = 무거래 분은 봉이 없음(직전가 유지 정책의 실측 "
          "근거). maxDD/RU는 구간 내 진행 고/저점 대비 누적치.")


if __name__ == "__main__":
    main()
