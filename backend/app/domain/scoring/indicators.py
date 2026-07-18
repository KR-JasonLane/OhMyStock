"""순수 지표 함수. 입력은 과거→최신 정렬된 list[Candle]과 위치 인덱스 at.
창이 데이터 범위를 벗어나면 None — 호출자(전략)는 None이면 신호 False 처리.
'당일 제외' 창([at-period..at-1])을 쓰는 함수는 돌파/눌림목처럼 "직전 구간
대비 오늘"을 비교하는 신호용이다 (스펙 §4-4 표)."""

from app.domain.broker import Candle


def moving_average(candles: list[Candle], period: int, at: int) -> float | None:
    if at - period + 1 < 0 or at >= len(candles):
        return None
    window = candles[at - period + 1:at + 1]
    return sum(c.close for c in window) / period


def period_return(candles: list[Candle], period: int, at: int) -> float | None:
    if at - period < 0 or at >= len(candles):
        return None
    base = candles[at - period].close
    return candles[at].close / base - 1


def rolling_high(candles: list[Candle], period: int, at: int) -> int | None:
    if at - period < 0 or at >= len(candles):
        return None
    return max(c.high for c in candles[at - period:at])


def average_volume(candles: list[Candle], period: int, at: int) -> float | None:
    if at - period < 0 or at >= len(candles):
        return None
    return sum(c.volume for c in candles[at - period:at]) / period


def max_close(candles: list[Candle], period: int, at: int) -> int | None:
    if at - period < 0 or at >= len(candles):
        return None
    return max(c.close for c in candles[at - period:at])
