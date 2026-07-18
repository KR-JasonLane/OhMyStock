"""엔진 손계산 검증 — 스텁 전략과 작은 설정으로 결정론적 시나리오."""

import math
from datetime import date, timedelta

import pytest

from app.domain.broker import Candle
from app.domain.scoring.config import ScoringConfig
from app.domain.scoring.engine import _normalize, run_scoring
from tests.scoring.test_indicators import make_candles

CFG = ScoringConfig(ma_short=2, ma_long=4, pullback_lookback=1,
                    min_bars=6, min_sector_members=1, top_sectors=1,
                    top_candidates=10, hold_days=2, min_signal_occurrences=1)


class AlwaysOn:
    name = "always"

    def signal(self, candles, at, cfg):
        return True


class AlwaysOff:
    name = "never"

    def signal(self, candles, at, cfg):
        return False


def rising(mult):  # 종가 10,20,...,80 × mult — 상승 시리즈
    return make_candles([10 * mult * i for i in range(1, 9)])


def flat():
    return make_candles([100] * 8)


def test_섹터_순위와_선정():
    result = run_scoring(
        members_by_sector={"S1": ["AAA111", "BBB222"], "S2": ["CCC333"]},
        sector_names={"S1": "강한업종", "S2": "횡보업종"},
        candles_by_symbol={"AAA111": rising(1), "BBB222": rising(2),
                           "CCC333": flat()},
        cfg=CFG, strategies=(AlwaysOn(),))
    by_code = {s.code: s for s in result.sectors}
    assert by_code["S1"].rank == 1 and by_code["S1"].selected is True
    assert by_code["S2"].rank == 2 and by_code["S2"].selected is False
    assert by_code["S1"].score == 1.0 and by_code["S2"].score == 0.0  # min-max
    # 후보는 선정 업종(S1) 소속만
    assert {c.symbol for c in result.candidates} == {"AAA111", "BBB222"}


def test_신호_없는_종목은_후보_제외():
    result = run_scoring(
        members_by_sector={"S1": ["AAA111"]}, sector_names={"S1": "업종"},
        candles_by_symbol={"AAA111": rising(1)},
        cfg=CFG, strategies=(AlwaysOff(),))
    assert result.candidates == ()


def test_봉_부족_종목은_제외되고_집계된다():
    short = make_candles([10, 11, 12])  # min_bars=6 미달
    result = run_scoring(
        members_by_sector={"S1": ["AAA111", "SHORT1"]},
        sector_names={"S1": "업종"},
        candles_by_symbol={"AAA111": rising(1), "SHORT1": short},
        cfg=CFG, strategies=(AlwaysOn(),))
    assert result.excluded_short_history == 1
    assert {c.symbol for c in result.candidates} == {"AAA111"}


def test_최소_구성종목_미달_업종은_섹터_계산에서_제외():
    cfg = ScoringConfig(ma_short=2, ma_long=4, pullback_lookback=1,
                        min_bars=6, min_sector_members=2, top_sectors=1,
                        hold_days=2, min_signal_occurrences=1)
    result = run_scoring(
        members_by_sector={"S1": ["AAA111"]}, sector_names={"S1": "업종"},
        candles_by_symbol={"AAA111": rising(1)},
        cfg=cfg, strategies=(AlwaysOn(),))
    assert result.sectors == () and result.candidates == ()


def test_중복_소속은_점수_높은_업종에_한번만():
    result = run_scoring(
        members_by_sector={"S1": ["AAA111", "BBB222"],
                           "S2": ["AAA111", "CCC333"]},
        sector_names={"S1": "강", "S2": "약"},
        candles_by_symbol={"AAA111": rising(3), "BBB222": rising(2),
                           "CCC333": flat()},
        cfg=ScoringConfig(ma_short=2, ma_long=4, pullback_lookback=1,
                          min_bars=6, min_sector_members=1, top_sectors=2,
                          hold_days=2, min_signal_occurrences=1),
        strategies=(AlwaysOn(),))
    mine = [c for c in result.candidates if c.symbol == "AAA111"]
    assert len(mine) == 1
    assert mine[0].sector_code == "S1"  # S1이 더 강한 업종


def test_봉이_아예_없는_종목도_봉_부족_제외로_집계된다():
    """candles_by_symbol에 아예 없는 종목(수집 누락)도 봉 부족(min_bars 미달)과
    동일하게 excluded_short_history에 집계돼야 한다 — run_scoring 호출자 계약."""
    result = run_scoring(
        members_by_sector={"S1": ["AAA111", "MISSING1"]},
        sector_names={"S1": "업종"},
        candles_by_symbol={"AAA111": rising(1)},  # MISSING1은 candles_by_symbol에 없음
        cfg=CFG, strategies=(AlwaysOn(),))
    assert result.excluded_short_history == 1
    assert {c.symbol for c in result.candidates} == {"AAA111"}


@pytest.mark.parametrize("kwargs", [
    dict(sector_weight_r20=0.5, sector_weight_r60=0.5, sector_weight_r5=0.5),  # 합 1.5
    dict(final_weight_sector=0.5, final_weight_strategy=0.6),                 # 합 1.1
    dict(hold_days=0),
    dict(min_bars=0),
], ids=["sector_weights", "final_weights", "hold_days", "min_bars"])
def test_설정_불변식_위반시_예외(kwargs):
    """섹터 가중합/최종 가중합이 1.0이 아니거나 hold_days/min_bars가 1 미만이면
    run_scoring 진입 시점에 즉시 실패해야 한다 (T4 아키텍처 패널 carry-over) —
    4개 위반 분기 전부."""
    bad_cfg = ScoringConfig(**kwargs)
    with pytest.raises(ValueError):
        run_scoring(members_by_sector={}, sector_names={},
                   candles_by_symbol={}, cfg=bad_cfg, strategies=())


def test_전략별_정규화는_풀링_정규화와_다르다():
    """섹터/전략 점수는 전략별로 독립 min-max 정규화된다 — 전략을 구분하지
    않고 값을 한데 모아 풀링 정규화했다면 아래 기대값과 달라진다."""
    min_bars = 6
    cfg = ScoringConfig(ma_short=2, ma_long=4, pullback_lookback=1,
                        min_bars=min_bars, min_sector_members=1, top_sectors=1,
                        top_candidates=10, hold_days=1, min_signal_occurrences=1)

    class EarlyStrategy:
        name = "early"

        def signal(self, candles, at, cfg):
            return at == min_bars - 1

    class LateStrategy:
        name = "late"

        def signal(self, candles, at, cfg):
            return at >= min_bars

    def build(early_close, late_close):
        rows = [(100, 100, 101, 99)] * min_bars  # 필러 6봉 (open,close,high,low)
        rows += [(100, early_close, max(100, early_close) + 1, 99),
                 (100, late_close, max(100, late_close) + 1, 99)]
        return [Candle(symbol="X", date=date(2026, 1, 1) + timedelta(days=i),
                       open=o, close=c, high=h, low=lo, volume=1000)
                for i, (o, c, h, lo) in enumerate(rows)]

    candles = {"AAA111": build(101, 150),   # R_early=0.01, R_late=0.50
              "BBB222": build(102, 200)}    # R_early=0.02, R_late=1.00
    result = run_scoring(
        members_by_sector={"S1": ["AAA111", "BBB222"]}, sector_names={"S1": "업종"},
        candles_by_symbol=candles, cfg=cfg,
        strategies=(EarlyStrategy(), LateStrategy()))

    detail = {(c.symbol, d.strategy): d.score
             for c in result.candidates for d in c.details}
    # 전략별 min-max: early [0.01,0.02]→{0,1}, late [0.50,1.00]→{0,1}.
    # 풀링(전략 구분 없이 4값 [0.01,0.02,0.50,1.00])이었다면 값이 달라진다
    # (예: BBB222/early 풀링 정규화=(0.02-0.01)/0.99≈0.0101 ≠ 전략별=1.0).
    assert math.isclose(detail[("AAA111", "early")], 0.0)
    assert math.isclose(detail[("BBB222", "early")], 1.0)
    assert math.isclose(detail[("AAA111", "late")], 0.0)
    assert math.isclose(detail[("BBB222", "late")], 1.0)


def test_게이트된_전략점수는_정규화_이후에도_0이다():
    """occurrences < min_signal_occurrences인 (symbol, strategy) 쌍은 정규화
    이후에도 점수 0으로 강제 클램프돼야 한다 — 그렇지 않으면 표본 부족(원점수 0)이
    검증된 음의 성과보다 상대 정규화에서 유리해지는 역전이 발생한다."""
    cfg = ScoringConfig(ma_short=1, ma_long=1, pullback_lookback=1,
                        min_bars=3, min_sector_members=1, top_sectors=1,
                        top_candidates=10, hold_days=1, min_signal_occurrences=2)

    # GATED1: 길이 == min_bars → simulate 유효 at 없음 → occurrences=0 (게이트).
    gated_candles = make_candles([10, 20, 30])
    # NEG1: AlwaysOn으로 5회 발생, 평균수익률<0·승률>0 (한 번은 상승) — 표본은
    # 충분하지만 실제로 검증된 약세.
    neg_closes = [100, 95, 90, 100, 80, 75, 70, 65]
    neg_opens = [100, 100, 95, 90, 100, 80, 75, 70]  # opens[i] = closes[i-1]
    neg_candles = [
        Candle(symbol="NEG1", date=date(2026, 1, 1) + timedelta(days=i),
               open=o, high=max(o, c) + 1, low=min(o, c) - 1, close=c, volume=1000)
        for i, (o, c) in enumerate(zip(neg_opens, neg_closes))]

    result = run_scoring(
        members_by_sector={"S1": ["GATED1", "NEG1"]}, sector_names={"S1": "업종"},
        candles_by_symbol={"GATED1": gated_candles, "NEG1": neg_candles},
        cfg=cfg, strategies=(AlwaysOn(),))

    by_symbol = {c.symbol: c for c in result.candidates}
    gated_detail = by_symbol["GATED1"].details[0]
    neg_detail = by_symbol["NEG1"].details[0]

    assert gated_detail.occurrences < cfg.min_signal_occurrences
    assert gated_detail.score == 0.0
    assert neg_detail.occurrences >= cfg.min_signal_occurrences
    assert neg_detail.avg_return < 0 and neg_detail.win_rate > 0
    assert neg_detail.score == 0.0  # 최저 원점수 → 정규화 후에도 0

    # 회귀 확인: 클램프 없이 순수 min-max만 적용했다면 원점수 0(게이트)이
    # 음의 원점수보다 높아 1.0으로 뒤바뀌었을 것 (역전 버그) — 실제 결과는 0.0.
    naive = _normalize([0.0, neg_detail.avg_return * neg_detail.win_rate])
    assert naive[0] == 1.0


def test_전부_게이트되면_전부_0점이다():
    """표본이 전부 부족하면 정규화의 hi==lo 분기(0.5)가 아니라 0.0으로
    클램프돼야 한다."""
    cfg = ScoringConfig(ma_short=1, ma_long=1, pullback_lookback=1,
                        min_bars=3, min_sector_members=1, top_sectors=1,
                        top_candidates=10, hold_days=1, min_signal_occurrences=1)
    # 두 종목 모두 길이 == min_bars → simulate 유효 at 없음 → occurrences=0.
    candles = {"AAA111": make_candles([10, 20, 30]),
              "BBB222": make_candles([50, 40, 30])}
    result = run_scoring(
        members_by_sector={"S1": ["AAA111", "BBB222"]}, sector_names={"S1": "업종"},
        candles_by_symbol=candles, cfg=cfg, strategies=(AlwaysOn(),))
    assert {c.symbol for c in result.candidates} == {"AAA111", "BBB222"}
    for c in result.candidates:
        assert c.details[0].occurrences == 0
        assert c.details[0].score == 0.0
