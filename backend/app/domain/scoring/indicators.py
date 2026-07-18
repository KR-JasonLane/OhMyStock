"""순수 지표 함수. 입력은 과거→최신 정렬된 list[Candle]과 위치 인덱스 at.
창이 데이터 범위를 벗어나면 None — 호출자(전략)는 None이면 신호 False 처리.
'당일 제외' 창([at-period..at-1])을 쓰는 함수는 돌파/눌림목처럼 "직전 구간
대비 오늘"을 비교하는 신호용이다 (스펙 §4-4 표).
at은 0 이상이며 "마감이 완결된 봉"의 인덱스라는 계약, 범위 밖(음수 포함)은 None."""

from app.domain.broker import Candle


def _in_range(candles: list[Candle], start: int, at: int) -> bool:
    """window의 시작 인덱스(start)가 음수가 아니고 at이 데이터 범위 안일 때만 True."""
    return start >= 0 and at < len(candles)


def moving_average(candles: list[Candle], period: int, at: int) -> float | None:
    start = at - period + 1
    if not _in_range(candles, start, at):
        return None
    window = candles[start:at + 1]
    return sum(c.close for c in window) / period


def period_return(candles: list[Candle], period: int, at: int) -> float | None:
    start = at - period
    if not _in_range(candles, start, at):
        return None
    base = candles[start].close
    return candles[at].close / base - 1


def rolling_high(candles: list[Candle], period: int, at: int) -> int | None:
    start = at - period
    if not _in_range(candles, start, at):
        return None
    return max(c.high for c in candles[start:at])


def average_volume(candles: list[Candle], period: int, at: int) -> float | None:
    start = at - period
    if not _in_range(candles, start, at):
        return None
    return sum(c.volume for c in candles[start:at]) / period


def max_close(candles: list[Candle], period: int, at: int) -> int | None:
    start = at - period
    if not _in_range(candles, start, at):
        return None
    return max(c.close for c in candles[start:at])
