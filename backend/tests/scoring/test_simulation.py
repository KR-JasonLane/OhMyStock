"""시뮬레이션 손계산 검증 — 고정 인덱스에서 켜지는 스텁 전략 사용."""

from app.domain.scoring.config import ScoringConfig
from app.domain.scoring.simulation import StrategyFitness, simulate
from tests.scoring.test_indicators import make_candles


class StubStrategy:
    name = "stub"

    def __init__(self, fire_at):
        self._fire_at = set(fire_at)

    def signal(self, candles, at, cfg):
        return at in self._fire_at


def test_시뮬레이션_수익률과_승률():
    # 신호 at=2 → 진입 open[3]=100, 청산 close[2+2=4]=110 → +10%
    # 신호 at=5 → 진입 open[6]=100, 청산 close[7]=95  → -5%
    closes = [50, 50, 50, 100, 110, 60, 100, 95, 90]
    cfg = ScoringConfig(hold_days=2, min_bars=1)
    fit = simulate(make_candles(closes), StubStrategy([2, 5]), cfg)
    assert fit.occurrences == 2
    assert abs(fit.avg_return - (0.10 + (-0.05)) / 2) < 1e-9
    assert fit.win_rate == 0.5


def test_잔여봉_부족한_신호는_표본_제외():
    closes = [50] * 9
    cfg = ScoringConfig(hold_days=2, min_bars=1)
    fit = simulate(make_candles(closes), StubStrategy([7]), cfg)  # 청산봉(9) 없음
    assert fit == StrategyFitness(avg_return=0.0, win_rate=0.0, occurrences=0)


def test_min_bars_이전_구간은_평가하지_않는다():
    closes = [50] * 10
    cfg = ScoringConfig(hold_days=2, min_bars=6)
    fit = simulate(make_candles(closes), StubStrategy([2]), cfg)  # at=2 < 5
    assert fit.occurrences == 0
