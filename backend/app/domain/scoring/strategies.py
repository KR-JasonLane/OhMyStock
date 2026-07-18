"""스윙 3전략의 진입 신호. 조건 정의는 스펙 §4-4 표와 1:1 — 변경은 스펙과 함께.
지표가 None(창 부족)이면 신호는 False. 상태 없는 구현 — 인스턴스 재사용 안전."""

from typing import Protocol, runtime_checkable

from app.domain.broker import Candle
from app.domain.scoring.config import ScoringConfig
from app.domain.scoring.indicators import (average_volume, max_close,
                                           moving_average, period_return,
                                           rolling_high)


@runtime_checkable
class Strategy(Protocol):
    name: str

    def signal(self, candles: list[Candle], at: int, cfg: ScoringConfig) -> bool: ...


class MomentumStrategy:
    """추세 지속: 종가 > MA20 > MA60 (정배열) AND R20 > 0."""

    name = "momentum"

    def signal(self, candles: list[Candle], at: int, cfg: ScoringConfig) -> bool:
        ma_s = moving_average(candles, cfg.ma_short, at)
        ma_l = moving_average(candles, cfg.ma_long, at)
        ret = period_return(candles, cfg.ma_short, at)
        if ma_s is None or ma_l is None or ret is None:
            return False
        return candles[at].close > ma_s > ma_l and ret > 0


class PullbackStrategy:
    """상승 추세 중 조정 후 MA20 밴드 복귀: 종가 > MA60 AND MA20 상승
    (MA20[t] > MA20[t-lookback]) AND 직전 lookback일 최고 종가 > 당일 종가
    AND 당일 종가가 MA20 ±band 내."""

    name = "pullback"

    def signal(self, candles: list[Candle], at: int, cfg: ScoringConfig) -> bool:
        ma_s_now = moving_average(candles, cfg.ma_short, at)
        ma_s_prev = moving_average(candles, cfg.ma_short,
                                   at - cfg.pullback_lookback)
        ma_l = moving_average(candles, cfg.ma_long, at)
        recent_high = max_close(candles, cfg.pullback_lookback, at)
        if (ma_s_now is None or ma_s_prev is None or ma_l is None
                or recent_high is None):
            return False
        close = candles[at].close
        return (close > ma_l
                and ma_s_now > ma_s_prev
                and recent_high > close
                and abs(close - ma_s_now) / ma_s_now <= cfg.pullback_band)


class BreakoutStrategy:
    """박스권 돌파: 종가 > 직전 breakout_lookback일 최고가(당일 제외)
    AND 당일 거래량 ≥ 직전 ma_short일 평균 거래량 × mult."""

    name = "breakout"

    def signal(self, candles: list[Candle], at: int, cfg: ScoringConfig) -> bool:
        box_high = rolling_high(candles, cfg.breakout_lookback, at)
        # 거래량 평균 창은 별도 파라미터 없이 cfg.ma_short(기본 20일)를 의도적으로
        # 재사용 — ma_short 변경 시 돌파 거래량 조건도 함께 바뀜(스펙 §4-6에
        # 별도 창 없음).
        avg_vol = average_volume(candles, cfg.ma_short, at)
        if box_high is None or avg_vol is None or avg_vol <= 0:
            return False
        c = candles[at]
        return c.close > box_high and c.volume >= avg_vol * cfg.breakout_volume_mult


def default_strategies() -> tuple[Strategy, ...]:
    return (MomentumStrategy(), PullbackStrategy(), BreakoutStrategy())
