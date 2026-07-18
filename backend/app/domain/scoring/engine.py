"""스코어링 순수 계산. I/O 없음 — 입력 데이터만으로 결정론적 결과를 만든다.
규칙 해석(정규화·중복 귀속·tie-break)은 계획서 Task 6 머리말과 스펙 §4-3~§4-5."""

import math
from dataclasses import dataclass
from typing import NamedTuple

from app.domain.broker import Candle
from app.domain.scoring.config import ScoringConfig
from app.domain.scoring.indicators import period_return
from app.domain.scoring.simulation import StrategyFitness, simulate
from app.domain.scoring.strategies import Strategy


@dataclass(frozen=True)
class SectorScore:
    code: str
    name: str
    r20: float
    r60: float
    r5: float
    score: float
    rank: int
    selected: bool


@dataclass(frozen=True)
class StrategyDetail:
    strategy: str
    signal: bool
    avg_return: float
    win_rate: float
    occurrences: int
    score: float


@dataclass(frozen=True)
class Candidate:
    """total_score = final_weight_sector×sector_score +
    final_weight_strategy×strategy_score_norm — 저장된 값만으로 재현 가능
    (스펙 §5). strategy_score는 정규화 전 원값(참고용, 후보 간 비교 불가)."""
    symbol: str
    sector_code: str
    rank: int
    total_score: float
    sector_score: float
    strategy_score: float
    strategy_score_norm: float
    details: tuple[StrategyDetail, ...]


@dataclass(frozen=True)
class ScoringResult:
    sectors: tuple[SectorScore, ...]
    candidates: tuple[Candidate, ...]
    excluded_short_history: int


class _SectorRaw(NamedTuple):
    code: str
    r20: float
    r60: float
    r5: float
    raw: float


class _CandidateRow(NamedTuple):
    symbol: str
    sector_code: str
    strategy_score: float
    details: tuple[StrategyDetail, ...]


def _validate_config(cfg: ScoringConfig) -> None:
    """진입 시점 불변식 가드 — 잘못된 설정으로 조용히 왜곡된 점수를 만들지
    않도록 fail-loud (아키텍처 패널 carry-over, T4)."""
    sector_weight_sum = cfg.sector_weight_r20 + cfg.sector_weight_r60 + cfg.sector_weight_r5
    if not math.isclose(sector_weight_sum, 1.0):
        raise ValueError(
            "sector_weight_r20 + sector_weight_r60 + sector_weight_r5 must equal "
            f"1.0, got {sector_weight_sum}")
    final_weight_sum = cfg.final_weight_sector + cfg.final_weight_strategy
    if not math.isclose(final_weight_sum, 1.0):
        raise ValueError(
            "final_weight_sector + final_weight_strategy must equal 1.0, "
            f"got {final_weight_sum}")
    if cfg.hold_days < 1:
        raise ValueError(f"hold_days must be >= 1, got {cfg.hold_days}")
    if cfg.min_bars < 1:
        raise ValueError(f"min_bars must be >= 1, got {cfg.min_bars}")


def _normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def _mean_return(symbols: list[str], candles_by_symbol: dict[str, list[Candle]],
                 period: int) -> float:
    returns = []
    for s in symbols:
        candles = candles_by_symbol[s]
        r = period_return(candles, period, len(candles) - 1)
        if r is not None:
            returns.append(r)
    return sum(returns) / len(returns) if returns else 0.0


def run_scoring(members_by_sector: dict[str, list[str]],
                sector_names: dict[str, str],
                candles_by_symbol: dict[str, list[Candle]],
                cfg: ScoringConfig,
                strategies: tuple[Strategy, ...]) -> ScoringResult:
    """호출자 계약: members_by_sector에 등장하는 유니버스 종목이
    candles_by_symbol에 아예 없으면(수집 누락 등) 봉 부족 제외
    (excluded_short_history)로 집계된다 — candles_by_symbol에 있지만 min_bars
    미만인 경우와 동일하게 취급."""
    _validate_config(cfg)

    # 1) 봉 부족 종목 제외 (고유 종목 기준 집계) — candles_by_symbol에 아예
    # 없는 종목(수집 누락)도 봉 부족과 동일하게 제외·집계한다.
    eligible = {s for s, c in candles_by_symbol.items() if len(c) >= cfg.min_bars}
    excluded = {s for members in members_by_sector.values() for s in members
                if s not in eligible}
    members = {code: [s for s in ms if s in eligible]
               for code, ms in members_by_sector.items()}
    members = {code: ms for code, ms in members.items()
               if len(ms) >= cfg.min_sector_members}

    # 2) 섹터 강도 → 정규화 → 순위 → 상위 K 선정
    codes = sorted(members)
    raw_rows: list[_SectorRaw] = []
    for code in codes:
        r20 = _mean_return(members[code], candles_by_symbol, cfg.ma_short)
        r60 = _mean_return(members[code], candles_by_symbol, cfg.ma_long)
        r5 = _mean_return(members[code], candles_by_symbol, cfg.pullback_lookback)
        raw = (cfg.sector_weight_r20 * r20 + cfg.sector_weight_r60 * r60
               + cfg.sector_weight_r5 * r5)
        raw_rows.append(_SectorRaw(code, r20, r60, r5, raw))
    norm = _normalize([row.raw for row in raw_rows])
    ordered = sorted(zip(raw_rows, norm), key=lambda x: (-x[1], x[0].code))
    sectors = tuple(
        SectorScore(code=row.code, name=sector_names.get(row.code, ""),
                    r20=row.r20, r60=row.r60, r5=row.r5, score=score,
                    rank=i + 1, selected=i < cfg.top_sectors)
        for i, (row, score) in enumerate(ordered))
    sector_score_of = {s.code: s.score for s in sectors}

    # 3) 선정 업종 종목 — 중복 소속은 섹터 점수 높은 쪽에 귀속
    assigned: dict[str, str] = {}
    for s in sectors:
        if not s.selected:
            continue
        for symbol in members[s.code]:
            cur = assigned.get(symbol)
            if cur is None or sector_score_of[s.code] > sector_score_of[cur]:
                assigned[symbol] = s.code

    # 4) 전략 평가 (전 대상 종목 × 전략) → 전략별 정규화
    symbols = sorted(assigned)
    evals: dict[str, dict[str, tuple[bool, StrategyFitness]]] = {}
    for symbol in symbols:
        candles = candles_by_symbol[symbol]
        per: dict[str, tuple[bool, StrategyFitness]] = {}
        for strat in strategies:
            fired = strat.signal(candles, len(candles) - 1, cfg)
            per[strat.name] = (fired, simulate(candles, strat, cfg))
        evals[symbol] = per
    # 표본 부족 게이트는 정규화 이후에도 0이어야 한다 — 상대 정규화가
    # "검증 부족"을 "검증된 약세"보다 유리하게 만드는 역전 방지
    # (스펙 §4-4-b 의도). gated 쌍을 기억해 뒀다가 정규화 후 강제로 0.0 클램프.
    detail_score: dict[tuple[str, str], float] = {}
    for strat in strategies:
        raws = []
        gated_symbols: set[str] = set()
        for symbol in symbols:
            _, fit = evals[symbol][strat.name]
            if fit.occurrences < cfg.min_signal_occurrences:
                gated_symbols.add(symbol)
                raws.append(0.0)
            else:
                raws.append(fit.avg_return * fit.win_rate)
        for symbol, score in zip(symbols, _normalize(raws)):
            detail_score[(symbol, strat.name)] = (
                0.0 if symbol in gated_symbols else score)

    # 5) 후보 합성: 신호 켜진 전략 점수 합 → 후보 간 정규화 → 최종 점수
    rows: list[_CandidateRow] = []
    for symbol in symbols:
        details = []
        for strat in strategies:
            fired, fit = evals[symbol][strat.name]
            details.append(StrategyDetail(
                strategy=strat.name, signal=fired, avg_return=fit.avg_return,
                win_rate=fit.win_rate, occurrences=fit.occurrences,
                score=detail_score[(symbol, strat.name)]))
        details = tuple(details)
        if not any(d.signal for d in details):
            continue
        strategy_score = sum(d.score for d in details if d.signal)
        rows.append(_CandidateRow(symbol, assigned[symbol], strategy_score, details))
    strat_norm = _normalize([row.strategy_score for row in rows])
    totals = [
        (cfg.final_weight_sector * sector_score_of[row.sector_code]
         + cfg.final_weight_strategy * sn, row, sn)
        for row, sn in zip(rows, strat_norm)]
    totals.sort(key=lambda x: (-x[0], x[1].symbol))
    candidates = tuple(
        Candidate(symbol=row.symbol, sector_code=row.sector_code, rank=i + 1,
                  total_score=total, sector_score=sector_score_of[row.sector_code],
                  strategy_score=row.strategy_score, strategy_score_norm=sn,
                  details=row.details)
        for i, (total, row, sn) in enumerate(totals[:cfg.top_candidates]))
    return ScoringResult(sectors=sectors, candidates=candidates,
                         excluded_short_history=len(excluded))
