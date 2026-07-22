"""매매 비용 계산(순수) — 스펙 §7의 체결비용 SSOT.

`AnalysisConfig.round_trip_cost_pct`(LLM 프롬프트 서술용 근사)와 **별개**다 —
같은 숫자를 공유하지 않는다(§7 SSOT 경계). 여기는 실제 원 단위 손익에 반영되는
계산이며 세율·수수료율은 TradingConfig 설정값(개정 대응).

한국 시장 비대칭(트레이더 패널 §7):
  - 위탁수수료는 매수·매도 **양쪽** 부과
  - 증권거래세(+농특세)는 **매도에만** 부과
  - 코스피/코스닥 세율 상이(설정 분리), **ETF는 거래세 면제**
  - market 값은 기존 `Instrument.market`("kospi"|"kosdaq"|"etf") 그대로 소비

원 단위 반올림: round() 사용. ⚠️ 브로커의 실제 절사/반올림 방식은 미확정 —
G3 실측(272,750원 1주: cmsn 950, tax 544)과 ±1원 오차 가능. **정확한 실현손익
대사는 kt00018 실측 필드(pur_cmsn/sell_cmsn/tax)가 있으면 그것을 우선**하고,
이 모듈은 사전 추정(사이징 수수료 버퍼)과 폴백 계산에 쓴다.

청산 판정(exit_rules)은 그로스 가격 기준이며 비용은 P&L 기록에만 반영한다
(§6-2·§7 그로스/넷 경계)."""

from dataclasses import dataclass

from app.domain.trading.config import TradingConfig

_KNOWN_MARKETS = ("kospi", "kosdaq", "etf")


@dataclass(frozen=True)
class TradeCost:
    buy_commission: int   # 매수 수수료 (원)
    sell_commission: int  # 매도 수수료 (원)
    sell_tax: int         # 매도 거래세+농특세 (원, ETF는 0)

    @property
    def total(self) -> int:
        return self.buy_commission + self.sell_commission + self.sell_tax


def _pct(amount: int, pct: float) -> int:
    return round(amount * pct / 100)


def round_trip_cost(market: str, buy_amount: int, sell_amount: int,
                    config: TradingConfig) -> TradeCost:
    """매수→매도 왕복 비용. market은 "kospi"|"kosdaq"|"etf" — 미지 값은
    fail-loud(조용히 0세율 적용하면 손익이 과대평가된다)."""
    if market not in _KNOWN_MARKETS:
        raise ValueError(f"unknown market for cost calc: {market!r}")
    if buy_amount < 0 or sell_amount < 0:
        raise ValueError(f"amounts must be non-negative: buy={buy_amount} sell={sell_amount}")
    if market == "etf":
        tax = 0  # ETF 증권거래세 면제
    elif market == "kospi":
        tax = _pct(sell_amount, config.tax_sell_kospi_pct)
    else:  # kosdaq
        tax = _pct(sell_amount, config.tax_sell_kosdaq_pct)
    return TradeCost(
        buy_commission=_pct(buy_amount, config.commission_buy_pct),
        sell_commission=_pct(sell_amount, config.commission_sell_pct),
        sell_tax=tax,
    )


def realized_pnl(market: str, buy_amount: int, sell_amount: int,
                 config: TradingConfig) -> int:
    """비용 반영 실현손익(원) = 매도대금 − 매수대금 − 왕복비용.
    trade_positions.realized_pnl(§9)에 기록된다 — Task 6b monitor가 청산
    체결 후 호출(계획서 소유권: costs 계산은 6b)."""
    cost = round_trip_cost(market, buy_amount, sell_amount, config)
    return sell_amount - buy_amount - cost.total
