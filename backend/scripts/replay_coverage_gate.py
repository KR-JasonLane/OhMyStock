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
from app.domain.trading.exit_rules import (crossed_above,
                                           crossed_below)
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
    "005935": "우선주", "012510": "결함응답", "028300": "코스닥",
    "001067": "저유동",
}


def _window_end(anchor: date, days: int) -> date:
    d, count = anchor, 0
    while count < days:
        if is_trading_day(d):
            count += 1
        d += timedelta(days=1)
    return d


_ENTRY_CUTOFF_MIN = 9 * 60 + 5   # 09:05 — 엔진 진입 창 시작(§6-3)


def _assumed_entry(series: list[MinuteCandle]) -> tuple[int, int]:
    """(가정 진입가, 진입 봉 인덱스) — 윈도우 첫 거래일 09:05 이후 첫
    체결가(엔진이 진입 창에서 관측할 현재가 근사). 크로스 탐색은 이
    인덱스부터 시작한다(트레이더 R7-패치 Minor — 09:05 이전 갭 구간은
    엔진이 물리적으로 겪을 수 없는 가짜 크로스). 09:05 이후 체결이
    없으면 첫 봉 폴백(저유동 — 근사임을 감수, 게이트는 스크리닝 도구)."""
    first_day = series[0].ts.date()
    for i, candle in enumerate(series):
        if candle.ts.date() == first_day and (
                candle.ts.hour * 60 + candle.ts.minute) >= _ENTRY_CUTOFF_MIN:
            return candle.close, i
    # 폴백은 침묵하지 않는다(아키텍트 R7-패치 Minor — 저유동 심볼에서
    # 조용히 걸리면 진입가 근사가 왜곡됐음을 사람이 못 알아챈다)
    print(f"  ⚠️ {series[0].symbol}: 첫날 09:05 이후 체결 없음 — 첫 봉"
          f"({series[0].ts:%H:%M}) 폴백 진입가 사용")
    return series[0].close, 0


def _stats(series: list[MinuteCandle]) -> tuple[float, float, str, str, str, int]:
    """(maxDD%, maxRU%, 첫 손절/익절/트레일링안착 크로스 시각, 정규장 결측 분).

    ⚠️ 크로스 기준 = **가정 진입가(윈도우 첫날 09:05 시점 가격) 대비**
    (R7 발견①, 2026-07-23): 이전 정의(진행 고점 대비 DD)는 엔진의 손절
    의미론(exit_rules — 진입가 대비 -stop_loss_pct)과 달라 심볼 선정이
    빗나갔다(035760 — 게이트 "11:00 크로스" vs 엔진 기준 당일 최저
    -4.45%로 미발동 실측). maxDD/RU는 참고용 진행 고/저점 통계로 유지."""
    entry_ref, entry_idx = _assumed_entry(series)
    peak = trough = series[0].close
    max_dd = max_ru = 0.0
    first_dd = first_ru = first_ru8 = "-"
    gaps = 0
    prev_ts = None
    for i, candle in enumerate(series):
        peak = max(peak, candle.close)
        trough = min(trough, candle.close)
        max_dd = max(max_dd, (peak - candle.close) / peak * 100)
        max_ru = max(max_ru, (candle.close - trough) / trough * 100)
        # 엔진 의미론 크로스(진입가 대비, **진입 봉부터** 탐색) — exit_rules
        # 의 공유 헬퍼로 판정(하드코딩 복제 금지). ⚠️ 크로스는 "가격이
        # 임계를 자극했다"는 커버리지 신호다 — 실제 청산 사유는 엔진
        # 우선순위(§6-2: 트레일링 래치 시 고정 익절 영구 비활성)에 따라
        # 다를 수 있다(트레이더 R7-패치 #2 — 예: +10% 크로스는 대개
        # TAKE_PROFIT이 아니라 TRAILING_STOP으로 청산됨).
        if i >= entry_idx:
            stamp = candle.ts.strftime("%m-%d %H:%M")
            if first_dd == "-" and crossed_below(candle.close, entry_ref,
                                                 _CFG.stop_loss_pct):
                first_dd = stamp
            if first_ru == "-" and crossed_above(candle.close, entry_ref,
                                                 _CFG.take_profit_pct):
                first_ru = stamp
            if first_ru8 == "-" and crossed_above(
                    candle.close, entry_ref, _CFG.trailing_widen_until_pct):
                first_ru8 = stamp
        # 정규장(09:00~15:20) 연속성 — 결측 분 실측(동시호가 구간 제외)
        if (prev_ts is not None and candle.ts.date() == prev_ts.date()
                and prev_ts.time().hour * 60 + prev_ts.time().minute >= 540
                and candle.ts.time().hour * 60 + candle.ts.time().minute <= 920):
            delta = int((candle.ts - prev_ts).total_seconds() // 60) - 1
            if delta > 0:
                gaps += delta
        prev_ts = candle.ts
    return max_dd, max_ru, first_dd, first_ru, first_ru8, gaps


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
        max_dd, max_ru, first_dd, first_ru, first_ru8, gaps = _stats(series)
        # 판정도 엔진 의미론(first_* — 진입가 대비)으로(개발자 R7-패치
        # Critical: 요약 줄이 옛 진행고점 정의로 남으면 표만 고친 반쪽 수정)
        any_dd |= first_dd != "-"
        any_ru8 |= first_ru8 != "-"
        any_ru10 |= first_ru != "-"
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
          "근거). maxDD/RU는 구간 내 진행 고/저점 대비 누적치(참고용).")
    print("  ※ 크로스='가격이 임계를 자극' 커버리지 — 실제 청산 사유는 "
          "엔진 우선순위에 따라 다를 수 있음(예: +10% 자극은 트레일링 "
          "래치로 대개 TRAILING_STOP 청산 — §6-2).")


if __name__ == "__main__":
    main()
