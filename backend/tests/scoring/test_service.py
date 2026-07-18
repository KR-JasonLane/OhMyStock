"""ScoringService 오케스트레이션 검증 — 가짜 store, 스텁 전략, 고정 기준일."""

from datetime import date

import pytest

from app.domain.scoring.config import ScoringConfig
from app.domain.scoring.service import ScoringService
from tests.scoring.test_indicators import make_candles

REF = date(2026, 7, 17)
CFG = ScoringConfig(ma_short=2, ma_long=4, pullback_lookback=1, min_bars=6,
                    min_sector_members=1, top_sectors=1, hold_days=2,
                    min_signal_occurrences=1, stale_exclusion_limit=0.05)


class AlwaysOn:
    name = "always"

    def signal(self, candles, at, cfg):
        return True


class FakeStore:
    def __init__(self, instruments, memberships, names, candles):
        self._instruments = instruments   # [(symbol, audit_info, state)]
        self._memberships = memberships
        self._names = names
        self._candles = candles
        self.finished = None
        self.saved = None

    def create_run(self, reference_date, config_json):
        return 1

    def finish_run(self, run_id, status, universe_count=0, stale_excluded=0,
                   failure_reason=None):
        self.finished = (status, universe_count, stale_excluded, failure_reason)

    def save_results(self, run_id, result):
        self.saved = result

    def active_common_instruments(self):
        return self._instruments

    def industry_memberships(self):
        return self._memberships, self._names

    def latest_dates(self, symbols):
        return {s: self._candles[s][-1].date for s in symbols
                if s in self._candles and self._candles[s]}

    def load_candles(self, symbols):
        return {s: self._candles[s] for s in symbols if s in self._candles}


def normal(symbol):
    return (symbol, "정상", "증거금100%")


@pytest.mark.anyio
async def test_성공_경로_결과_저장():
    candles = make_candles([10, 20, 30, 40, 50, 60, 70, 80])  # 최신 = 기준일
    store = FakeStore([normal("AAA111")], {"005": ["AAA111"]},
                      {"005": "음식료"}, {"AAA111": candles})
    service = ScoringService(store, config=CFG, strategies=(AlwaysOn(),),
                             reference_provider=lambda: candles[-1].date)
    await service.run()
    assert store.finished[0] == "succeeded"
    assert store.saved is not None
    assert store.saved.candidates[0].symbol == "AAA111"


@pytest.mark.anyio
async def test_유니버스_필터_비정상_상태_제외():
    candles = make_candles([10, 20, 30, 40, 50, 60, 70, 80])
    store = FakeStore(
        [normal("AAA111"), ("BBB222", "관리종목", "관리종목"),
         ("CCC333", "정상", "증거금100%|거래정지")],
        {"005": ["AAA111", "BBB222", "CCC333"]}, {"005": "음식료"},
        {s: candles for s in ("AAA111", "BBB222", "CCC333")})
    service = ScoringService(store, config=CFG, strategies=(AlwaysOn(),),
                             reference_provider=lambda: candles[-1].date)
    await service.run()
    assert store.finished[0] == "succeeded"
    assert store.finished[1] == 1  # universe_count: AAA111만
    assert {c.symbol for c in store.saved.candidates} == {"AAA111"}


@pytest.mark.anyio
async def test_신선도_게이트_전체_실패():
    stale = make_candles([10, 20, 30, 40, 50, 60, 70, 80])  # 마지막 날짜 < 기준일
    store = FakeStore([normal("AAA111")], {"005": ["AAA111"]},
                      {"005": "음식료"}, {"AAA111": stale})
    future_ref = date(2099, 1, 1)
    service = ScoringService(store, config=CFG, strategies=(AlwaysOn(),),
                             reference_provider=lambda: future_ref)
    await service.run()
    assert store.finished[0] == "failed"
    assert "stale" in store.finished[3]
    assert store.saved is None


@pytest.mark.anyio
async def test_소수_정체_종목은_개별_제외되고_집계된다():
    fresh = make_candles([10, 20, 30, 40, 50, 60, 70, 80])
    cfg = ScoringConfig(ma_short=2, ma_long=4, pullback_lookback=1, min_bars=6,
                        min_sector_members=1, top_sectors=1, hold_days=2,
                        min_signal_occurrences=1,
                        stale_exclusion_limit=0.5)  # 50%까지 허용
    stale = make_candles([10, 20, 30, 40, 50, 60, 70])  # 하루 짧음
    store = FakeStore([normal("AAA111"), normal("BBB222")],
                      {"005": ["AAA111", "BBB222"]}, {"005": "음식료"},
                      {"AAA111": fresh, "BBB222": stale})
    service = ScoringService(store, config=cfg, strategies=(AlwaysOn(),),
                             reference_provider=lambda: fresh[-1].date)
    await service.run()
    assert store.finished[0] == "succeeded"
    assert store.finished[2] == 1  # stale_excluded
    assert {c.symbol for c in store.saved.candidates} == {"AAA111"}


@pytest.mark.anyio
async def test_빈_유니버스는_실패():
    store = FakeStore([], {}, {}, {})
    service = ScoringService(store, config=CFG, strategies=(AlwaysOn(),),
                             reference_provider=lambda: REF)
    await service.run()
    assert store.finished[0] == "failed"
    assert "universe" in store.finished[3]


@pytest.mark.anyio
async def test_start는_중복_실행을_거부():
    candles = make_candles([10, 20, 30, 40, 50, 60, 70, 80])
    store = FakeStore([normal("AAA111")], {"005": ["AAA111"]},
                      {"005": "음식료"}, {"AAA111": candles})
    service = ScoringService(store, config=CFG, strategies=(AlwaysOn(),),
                             reference_provider=lambda: candles[-1].date)
    task = service.start()
    assert task is not None
    assert service.start() is None
    await task
