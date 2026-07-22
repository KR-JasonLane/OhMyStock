"""costs.py — 한국 시장 비대칭(매수/매도 수수료, 매도세, 시장별, ETF 면세) 검증."""

import pytest

from app.domain.trading.config import TradingConfig
from app.domain.trading.costs import TradeCost, realized_pnl, round_trip_cost

CFG = TradingConfig(max_single_order_krw=1_000_000, max_daily_orders=20,
                    max_daily_order_krw=10_000_000,
                    min_avg_trading_value_krw=100_000_000,
                    commission_buy_pct=0.35, commission_sell_pct=0.35,
                    tax_sell_kospi_pct=0.20, tax_sell_kosdaq_pct=0.15)


def test_kospi_왕복_비용_비대칭():
    # 수수료는 양쪽, 세금은 매도에만 (트레이더 패널 §7)
    c = round_trip_cost("kospi", buy_amount=272_750, sell_amount=272_500, config=CFG)
    assert c.buy_commission == round(272_750 * 0.0035)   # 955
    assert c.sell_commission == round(272_500 * 0.0035)  # 954
    assert c.sell_tax == round(272_500 * 0.0020)         # 545
    assert c.total == c.buy_commission + c.sell_commission + c.sell_tax


def test_kosdaq은_별도_세율():
    c = round_trip_cost("kosdaq", 100_000, 100_000, CFG)
    assert c.sell_tax == round(100_000 * 0.0015)  # 코스닥 세율 0.15%
    assert c.buy_commission == c.sell_commission == round(100_000 * 0.0035)


def test_etf는_거래세_면제():
    c = round_trip_cost("etf", 100_000, 110_000, CFG)
    assert c.sell_tax == 0
    assert c.buy_commission > 0 and c.sell_commission > 0  # 수수료는 부과


def test_미지_market은_fail_loud():
    # 조용히 0세율 적용하면 손익 과대평가 — 반드시 예외
    with pytest.raises(ValueError, match="unknown market"):
        round_trip_cost("nasdaq", 100_000, 100_000, CFG)


def test_음수_금액은_ValueError():
    with pytest.raises(ValueError, match="non-negative"):
        round_trip_cost("kospi", -1, 100_000, CFG)


def test_realized_pnl은_비용을_반영한다():
    # 이익 거래: 매도대금 − 매수대금 − 왕복비용
    pnl = realized_pnl("kospi", buy_amount=100_000, sell_amount=110_000, config=CFG)
    cost = round_trip_cost("kospi", 100_000, 110_000, CFG)
    assert pnl == 110_000 - 100_000 - cost.total
    # 손실 거래도 대칭 (비용이 손실을 키움)
    pnl_loss = realized_pnl("kospi", 100_000, 95_000, CFG)
    assert pnl_loss < -5_000  # 가격 손실 5,000 + 비용


def test_G3_실측_규모_근사():
    """G3 실측(2026-07-22, 삼성전자 272,750 매수: pur_cmsn=950, tax=544)과
    같은 자릿수인지 — 브로커 반올림 방식 미확정이라 ±수 원 오차 허용
    (정확 대사는 kt00018 실측 필드 우선, costs는 사전 추정용 — 모듈 docstring)."""
    c = round_trip_cost("kospi", 272_750, 272_500, CFG)
    assert abs(c.buy_commission - 950) <= 10
    assert abs(c.sell_tax - 544) <= 10


def test_TradeCost_total():
    assert TradeCost(100, 200, 50).total == 350
