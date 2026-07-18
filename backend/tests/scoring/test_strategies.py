"""전략 신호 손계산 검증 — 작은 지표 창 설정 주입."""

from app.domain.scoring.config import ScoringConfig
from app.domain.scoring.strategies import (BreakoutStrategy, MomentumStrategy,
                                           PullbackStrategy,
                                           default_strategies)
from tests.scoring.test_indicators import make_candles

SMALL = ScoringConfig(ma_short=2, ma_long=4)


def test_모멘텀_정배열_상승이면_켜진다():
    # at=7: MA2=13.5, MA4=12.5, close=14 → 14>13.5>12.5, R2=14/12-1>0
    candles = make_candles([10, 10, 10, 10, 11, 12, 13, 14])
    assert MomentumStrategy().signal(candles, 7, SMALL) is True


def test_모멘텀_횡보면_꺼진다():
    candles = make_candles([10] * 8)   # close > MA 불성립
    assert MomentumStrategy().signal(candles, 7, SMALL) is False


def test_모멘텀_창부족이면_꺼진다():
    candles = make_candles([10, 11, 12])   # ma_long=4 계산 불가
    assert MomentumStrategy().signal(candles, 2, SMALL) is False


def test_눌림목_조정후_밴드복귀면_켜진다():
    cfg = ScoringConfig(ma_short=3, ma_long=5, pullback_lookback=3,
                        pullback_band=0.05)
    closes = [100, 110, 120, 130, 140, 150, 143]
    # at=6: MA5=mean(120,130,140,150,143)=136.6 < 143 ✓
    #       MA3[6]=mean(150,143,140)=144.33 > MA3[3]=mean(110,120,130)=120 ✓
    #       직전 3일 최고 종가 150 > 143 (조정 존재) ✓
    #       |143-144.33|/144.33 = 0.92% ≤ 5% (밴드 내) ✓
    assert PullbackStrategy().signal(make_candles(closes), 6, cfg) is True


def test_눌림목_추세이탈이면_꺼진다():
    cfg = ScoringConfig(ma_short=3, ma_long=5, pullback_lookback=3,
                        pullback_band=0.05)
    closes = [100, 110, 120, 130, 140, 150, 120]
    # at=6: 급락으로 종가(120)가 MA5(132) 아래 — 추세 조건에서 꺼짐
    # (밴드 조건까지 도달하지 않음).
    assert PullbackStrategy().signal(make_candles(closes), 6, cfg) is False


def test_눌림목_밴드만_이탈하면_꺼진다():
    """다른 조건은 전부 참으로 고정하고 밴드만 판별하는 쌍 테스트.
    closes=[100,110,120,130,140,150,138], at=6:
    MA5 = (120+130+140+150+138)/5 = 135.6 < 138 (추세 ✓)
    MA3[6] = (140+150+138)/3 = 142.67 > MA3[3] = (110+120+130)/3 = 120 (상승 ✓)
    직전 3일 최고 종가 150 > 138 (조정 존재 ✓)
    이탈률 |138-142.67|/142.67 = 3.27% → band=0.05면 켜지고 band=0.02면 꺼진다."""
    closes = [100, 110, 120, 130, 140, 150, 138]
    in_band = ScoringConfig(ma_short=3, ma_long=5, pullback_lookback=3,
                            pullback_band=0.05)
    out_band = ScoringConfig(ma_short=3, ma_long=5, pullback_lookback=3,
                             pullback_band=0.02)
    assert PullbackStrategy().signal(make_candles(closes), 6, in_band) is True
    assert PullbackStrategy().signal(make_candles(closes), 6, out_band) is False


def test_돌파_신고가_거래량_실리면_켜진다():
    # at=3: 직전 3일 고가 최대 20 < close 21,
    #       당일 거래량 300 ≥ 직전 2일 평균(100,150)=125 × 1.5 = 187.5
    candles = make_candles([10, 12, 11, 21], volumes=[100, 100, 150, 300],
                           highs=[15, 20, 16, 22])
    cfg = ScoringConfig(ma_short=2, breakout_lookback=3,
                        breakout_volume_mult=1.5)
    assert BreakoutStrategy().signal(candles, 3, cfg) is True


def test_돌파_거래량_부족이면_꺼진다():
    candles = make_candles([10, 12, 11, 21], volumes=[100, 100, 150, 100],
                           highs=[15, 20, 16, 22])
    cfg = ScoringConfig(ma_short=2, breakout_lookback=3,
                        breakout_volume_mult=1.5)
    assert BreakoutStrategy().signal(candles, 3, cfg) is False


def test_기본_전략_세트():
    assert [s.name for s in default_strategies()] == [
        "momentum", "pullback", "breakout"]
