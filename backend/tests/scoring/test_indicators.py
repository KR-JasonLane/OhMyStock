"""지표 함수 손계산 검증. 캔들 헬퍼는 Candle 검증 규칙(고가≥max(시,종),
저가≤min(시,종), 전부 양수)을 지키도록 만든다."""

from datetime import date, timedelta

from app.domain.broker import Candle
from app.domain.scoring.indicators import (average_volume, max_close,
                                           moving_average, period_return,
                                           rolling_high)


def make_candles(closes, volumes=None, highs=None, opens=None):
    volumes = volumes or [1000] * len(closes)
    highs = highs or [c + 1 for c in closes]
    opens = opens or list(closes)
    return [Candle(symbol="TEST00", date=date(2026, 1, 1) + timedelta(days=i),
                   open=o, high=max(h, o, c), low=min(o, c), close=c, volume=v)
            for i, (c, v, h, o) in enumerate(zip(closes, volumes, highs, opens))]


def test_이동평균():
    candles = make_candles([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert moving_average(candles, 3, 9) == 9.0        # (8+9+10)/3
    assert moving_average(candles, 10, 9) == 5.5
    assert moving_average(candles, 11, 9) is None      # 창 부족


def test_기간_수익률():
    candles = make_candles([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert period_return(candles, 2, 9) == 10 / 8 - 1  # 0.25
    assert period_return(candles, 9, 9) == 9.0         # 10/1-1
    assert period_return(candles, 10, 9) is None


def test_직전_고가_당일_제외():
    candles = make_candles([5, 5, 5, 5], highs=[10, 20, 15, 99])
    assert rolling_high(candles, 3, 3) == 20            # 인덱스 0~2, 당일(99) 제외
    assert rolling_high(candles, 4, 3) is None


def test_평균_거래량_당일_제외():
    candles = make_candles([5, 5, 5, 5], volumes=[100, 200, 300, 9999])
    assert average_volume(candles, 3, 3) == 200.0
    assert average_volume(candles, 4, 3) is None


def test_직전_최고_종가_당일_제외():
    candles = make_candles([10, 30, 20, 25])
    assert max_close(candles, 3, 3) == 30
    assert max_close(candles, 4, 3) is None


def test_config_기본값():
    from app.domain.scoring.config import ScoringConfig
    cfg = ScoringConfig()
    assert (cfg.top_sectors, cfg.top_candidates, cfg.hold_days) == (5, 20, 10)
    assert cfg.sector_weight_r20 + cfg.sector_weight_r60 + cfg.sector_weight_r5 == 1.0


def test_범위_밖_at은_None():
    candles = make_candles([1, 2, 3, 4, 5])
    assert moving_average(candles, 3, len(candles)) is None   # at == len(candles)
    assert moving_average(candles, 3, -1) is None              # 음수 at
    assert period_return(candles, 2, -1) is None
    assert rolling_high(candles, 2, -1) is None


def test_config_to_json_왕복():
    import json

    from app.domain.scoring.config import ScoringConfig
    cfg = ScoringConfig()
    assert json.loads(cfg.to_json())["hold_days"] == 10
    assert cfg.to_json() == cfg.to_json()
    assert cfg.final_weight_sector + cfg.final_weight_strategy == 1.0
