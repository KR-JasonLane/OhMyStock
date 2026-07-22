"""인메모리 모의 계좌(스펙 §4/§7) — 예수금/보유/미체결/체결 이력.

수수료·세금은 모의서버 실측률 재현(§7): 위탁수수료 0.35%(매수·매도 각각),
매도 거래세 0.2%(ETF 면제) — G3 실측(pur_cmsn/sell_cmsn 950, tax 544 @
272,750×1주)과 동일 방향. 반올림은 round(±1원 오차 실측 허용 범위).

이 모듈은 kt00001/kt00018/ka10075 응답의 **값 원천**이다 — 응답 형태(제로
패딩·A프리픽스 등)는 api/ 계층이 담당하고, 여기는 정수 상태만 유지한다."""

import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

COMMISSION_PCT = 0.35   # 모의 실측 ≈0.348% — §7
TAX_SELL_PCT = 0.20     # 코스피/코스닥 공통(모의 실측), ETF 면제


def _pct(amount: int, pct: float) -> int:
    return round(amount * pct / 100)


@dataclass
class Holding:
    symbol: str
    market: str            # "kospi"|"kosdaq"|"etf" — 세금 계산
    quantity: int
    total_cost: int        # 매입금액 누계(평단 산출용)

    @property
    def avg_price(self) -> int:
        return self.total_cost // self.quantity if self.quantity else 0


@dataclass
class OpenOrder:
    order_no: str
    symbol: str
    side: str              # "buy"|"sell"
    style: str             # "limit"|"market"
    quantity: int
    unfilled: int
    price: int             # limit 가격(시장가 0)
    submitted_at: datetime  # 벽시계(전파 지연 판정용 — §8 벽시계 기준)
    visible_after: datetime  # 이 시각 전에는 ka10075에 미노출(전파 지연 재현)


@dataclass
class Account:
    cash: int
    holdings: dict[str, Holding] = field(default_factory=dict)
    open_orders: dict[str, OpenOrder] = field(default_factory=dict)
    last_eval_missing: int = 0   # 직전 eval_total의 시세 결측 심볼 수(침묵 금지)
    cost_drift_total: int = 0    # 평단 절삭 누적 드리프트(원 — 가시화 계약)
    _seq: int = 0

    def next_order_no(self) -> str:
        self._seq += 1
        return f"R{self._seq:07d}"

    # ── 체결 반영(매칭 엔진이 호출) ─────────────────────────────────────

    def apply_buy_fill(self, symbol: str, market: str, quantity: int,
                       price: int) -> None:
        amount = price * quantity
        fee = _pct(amount, COMMISSION_PCT)
        self.cash -= amount + fee
        holding = self.holdings.get(symbol)
        if holding is None:
            self.holdings[symbol] = Holding(symbol, market, quantity, amount)
        else:
            holding.quantity += quantity
            holding.total_cost += amount

    def apply_sell_fill(self, symbol: str, quantity: int, price: int) -> None:
        holding = self.holdings.get(symbol)
        if holding is None or holding.quantity < quantity:
            raise ValueError(f"oversell {symbol}: have="
                             f"{holding.quantity if holding else 0} "
                             f"sell={quantity}")
        amount = price * quantity
        fee = _pct(amount, COMMISSION_PCT)
        tax = 0 if holding.market == "etf" else _pct(amount, TAX_SELL_PCT)
        self.cash += amount - fee - tax
        # 평단 유지 매도(total_cost 비례 차감). ⚠️ 정수 절삭 평단(//)의 반복
        # 매매 누적 드리프트(트레이더 R2 #3 실측: 2000회 교차 시 최대 ~168원)
        # — 전량 청산 시 잔여를 가시화하고 0으로 리셋(침묵 폐기 금지: 이
        # 계좌는 reconcile 검증의 판정 기준이라 오차가 오탐/은폐를 만든다).
        holding.total_cost -= holding.avg_price * quantity
        holding.quantity -= quantity
        if holding.quantity == 0:
            if holding.total_cost != 0:
                self.cost_drift_total += abs(holding.total_cost)
                logger.warning(
                    "avg-price truncation drift on full close %s: %d KRW "
                    "residual (cumulative %d)", symbol, holding.total_cost,
                    self.cost_drift_total)
            del self.holdings[symbol]

    # ── 조회 표면(응답 값 원천) ─────────────────────────────────────────

    def visible_open_orders(self, wall_now: datetime) -> list[OpenOrder]:
        """전파 지연(§8) 반영 — visible_after 이전 주문은 미노출(C1 재현)."""
        return [o for o in self.open_orders.values()
                if o.visible_after <= wall_now and o.unfilled > 0]

    def eval_total(self, prices: dict[str, int]) -> tuple[int, int]:
        """(총평가금액, 총평가손익) — kt00018 최상위 필수 필드(broker-api #6).
        시세 결측 심볼은 평단가로 평가하되 **카운트로 표면화**(침묵 금지 —
        개발자 R2 Minor. 재생 경로는 직전가 유지 정책상 결측이 없어야 정상)."""
        total_eval = 0
        total_profit = 0
        self.last_eval_missing = 0
        for holding in self.holdings.values():
            price = prices.get(holding.symbol)
            if price is None:
                price = holding.avg_price
                self.last_eval_missing += 1
            total_eval += price * holding.quantity
            total_profit += (price - holding.avg_price) * holding.quantity
        return total_eval, total_profit
