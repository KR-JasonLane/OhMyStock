"""TradingConfig — 스펙 §6-2 표와 1:1, §6-2-c fail-fast 검증 경계값 전수."""

import json
from datetime import time

import pytest

from app.domain.trading.config import TradingConfig

# 버그 봉쇄 한도(설정 필수 4개)의 테스트 공통값
REQUIRED = dict(max_single_order_krw=1_000_000, max_daily_orders=20,
                max_daily_order_krw=10_000_000, min_avg_trading_value_krw=100_000_000)


def cfg(**overrides) -> TradingConfig:
    return TradingConfig(**{**REQUIRED, **overrides})


def test_기본값이_스펙_표와_일치한다():
    c = cfg()
    assert c.stop_loss_pct == 5.0 and c.take_profit_pct == 10.0
    assert c.trailing_activate_pct == 5.0 and c.trailing_stop_pct == 3.0
    assert c.trailing_stop_wide_pct == 5.0 and c.trailing_widen_until_pct == 8.0
    assert c.max_holding_days == 10 and c.max_positions == 5
    assert c.max_capital_pct == 50.0 and c.signal_gap_guard_pct == 3.0
    assert c.entry_window_start == time(9, 5) and c.entry_window_end == time(9, 30)
    assert c.limit_order_timeout_sec == 60.0 and c.exit_limit_timeout_sec == 5.0
    assert c.poll_interval_sec == 1.0 and c.quote_failure_threshold == 5
    assert c.reentry_cooldown_min == 30


def test_버그봉쇄_한도는_기본값이_없다():
    # 설정 필수(스펙 §8-1) — 누락 시 TypeError로 기동 불가
    with pytest.raises(TypeError):
        TradingConfig()  # type: ignore[call-arg]


@pytest.mark.parametrize("field,value", [
    ("stop_loss_pct", 0.0), ("stop_loss_pct", 100.0), ("stop_loss_pct", -1.0),
    ("take_profit_pct", 0.0), ("take_profit_pct", -5.0),
    ("trailing_activate_pct", -0.1),
    ("trailing_stop_pct", 0.0),               # narrow > 0 필요
    ("trailing_stop_wide_pct", 2.0),          # 기본 narrow=3.0 > wide=2.0 위반
    ("trailing_widen_until_pct", 4.0),        # 기본 activate=5.0 > widen_until 위반
    ("max_holding_days", 0),
    ("max_positions", 0),
    ("max_capital_pct", 0.0), ("max_capital_pct", 100.1),
    ("signal_gap_guard_pct", -1.0),
    ("limit_order_timeout_sec", 0.0), ("exit_limit_timeout_sec", -1.0),
    ("poll_interval_sec", 0.0),
    ("quote_failure_threshold", 0),
    ("reentry_cooldown_min", -1),
    ("max_single_order_krw", 0),
    ("max_daily_orders", 0),
    ("min_avg_trading_value_krw", -1),
    ("commission_buy_pct", -0.1), ("commission_sell_pct", 100.0),
    ("tax_sell_kospi_pct", -0.1), ("tax_sell_kosdaq_pct", 100.0),
])
def test_범위_밖_값은_ValueError(field, value):
    with pytest.raises(ValueError, match="TradingConfig 검증 실패"):
        cfg(**{field: value})


def test_진입창_역전은_ValueError():
    with pytest.raises(ValueError, match="진입 창"):
        cfg(entry_window_start=time(10, 0), entry_window_end=time(9, 30))


def test_익절이_트레일링_활성화_이하면_ValueError():
    """역전되면 트레일링이 켜지기 전에 고정 익절이 걸려 결정 #29 v2의 추세
    추종 경로가 도달 불가능 — 개별 범위 검증으로 못 잡는 조합(트레이더 패널)."""
    with pytest.raises(ValueError, match="take_profit_pct"):
        cfg(take_profit_pct=4.0)  # 기본 activate=5.0보다 작음
    with pytest.raises(ValueError, match="take_profit_pct"):
        cfg(take_profit_pct=5.0)  # 동치도 금지(활성화 순간 익절 경합)


def test_일일_상한이_단건_상한보다_작으면_ValueError():
    with pytest.raises(ValueError, match="daily"):
        cfg(max_daily_order_krw=500_000)  # 단건 1_000_000보다 작음


def test_trailing_경계_동치는_허용():
    # narrow == wide (2단계 폭을 사실상 단일 폭으로) — 유효
    c = cfg(trailing_stop_pct=3.0, trailing_stop_wide_pct=3.0)
    assert c.trailing_stop_pct == c.trailing_stop_wide_pct
    # widen_until == activate (보간 구간 0) — 유효
    c2 = cfg(trailing_widen_until_pct=5.0)
    assert c2.trailing_widen_until_pct == c2.trailing_activate_pct


def test_to_json은_결정적_스냅샷을_만든다():
    data = json.loads(cfg().to_json())
    assert data["stop_loss_pct"] == 5.0
    assert data["entry_window_start"] == "09:05:00"  # time 직렬화
    assert data["entry_window_end"] == "09:30:00"
    assert data["max_single_order_krw"] == 1_000_000
