"""전략 적합도: 과거 신호 발생 건마다 't+1 시가 매수 → 매수일 포함
hold_days거래일째 종가 청산'을 시뮬레이션 (스펙 §4-4-b).

- 잔여 봉 부족(청산봉 없음) 발생 건은 표본에서 제외.
- 중복 보유 허용 (발생 건별 독립 — 표본 수 확보와 단순성 우선).
- TP/SL 반영형은 Phase 5에서 정책 확정 후 고도화 (스펙 §2 비범위)."""

from dataclasses import dataclass

from app.domain.broker import Candle
from app.domain.scoring.config import ScoringConfig
from app.domain.scoring.strategies import Strategy


@dataclass(frozen=True)
class StrategyFitness:
    avg_return: float
    win_rate: float
    occurrences: int


def simulate(candles: list[Candle], strategy: Strategy,
             cfg: ScoringConfig) -> StrategyFitness:
    returns: list[float] = []
    # 진입 인덱스 = at+1 (다음날 시가), 청산 인덱스 = (at+1)+(hold_days-1) = at+hold_days
    for at in range(cfg.min_bars - 1, len(candles)):
        exit_idx = at + cfg.hold_days
        if exit_idx >= len(candles):
            break  # 이후 at은 전부 잔여 봉 부족
        if not strategy.signal(candles, at, cfg):
            continue
        entry = candles[at + 1].open
        if entry <= 0:
            continue
        returns.append(candles[exit_idx].close / entry - 1)
    if not returns:
        return StrategyFitness(avg_return=0.0, win_rate=0.0, occurrences=0)
    wins = sum(1 for r in returns if r > 0)
    return StrategyFitness(avg_return=sum(returns) / len(returns),
                           win_rate=wins / len(returns),
                           occurrences=len(returns))
