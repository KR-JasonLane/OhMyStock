"""엔진 손계산 검증 — 스텁 전략과 작은 설정으로 결정론적 시나리오."""

import pytest

from app.domain.scoring.config import ScoringConfig
from app.domain.scoring.engine import run_scoring
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


def test_설정_불변식_위반시_예외():
    """섹터 가중합/최종 가중합이 1.0이 아니거나 hold_days/min_bars가 1 미만이면
    run_scoring 진입 시점에 즉시 실패해야 한다 (T4 아키텍처 패널 carry-over)."""
    bad_cfg = ScoringConfig(sector_weight_r20=0.5, sector_weight_r60=0.5,
                            sector_weight_r5=0.5)  # 합 1.5
    with pytest.raises(ValueError):
        run_scoring(members_by_sector={}, sector_names={},
                   candles_by_symbol={}, cfg=bad_cfg, strategies=())
