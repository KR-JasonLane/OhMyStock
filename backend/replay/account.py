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


def commission(amount: int) -> int:
    """위탁수수료(모의 실측률) — api 계층의 kt00018 수수료 필드 산출 공용."""
    return _pct(amount, COMMISSION_PCT)


def sell_tax(amount: int, market: str) -> int:
    """매도 거래세(ETF 면제 — 모의 실측률)."""
    return 0 if market == "etf" else _pct(amount, TAX_SELL_PCT)


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
    # 예약 단가(매수: limit가 or 접수 시점 현재가, 매도: 0) — 접수 시점에
    # 고정해 이후 시세 결측이 예약액을 침묵 0원으로 만들 수 없게 한다
    # (개발자 R3 델타 #2 — 결측 케이스의 구조적 제거. 실서버도 접수 시점에
    # ord_alow_amt를 차감한다). 시장가의 접수가 근사는 스펙 §2 잔여 한계.
    reserve_price: int = 0


@dataclass
class Account:
    cash: int
    holdings: dict[str, Holding] = field(default_factory=dict)
    open_orders: dict[str, OpenOrder] = field(default_factory=dict)
    last_eval_missing: int = 0   # 직전 eval_total의 시세 결측 심볼 수(침묵 금지)
    cost_drift_total: int = 0    # 평단 절삭 누적 드리프트(원 — 가시화 계약)
    negative_cash_events: int = 0  # 음수 예수금(§2 — 예약 근사 한계의 최후 방어선)
    _seq: int = 0

    def next_order_no(self) -> str:
        # 실측 형태: ka10075 ord_no='0034447' — 7자리 제로패딩 숫자 문자열
        # (R4 형태 재현: R-프리픽스 등 목 티가 나는 형식은 프로덕션 파서의
        # 암묵 가정을 검증하지 못한다)
        self._seq += 1
        return f"{self._seq:07d}"

    def estimate_buy_cost(self, price: int, quantity: int) -> int:
        """매수 소요 추정(금액+수수료) — 접수 시점 예수금 검사용(§8).
        수수료 수식은 Account가 소유(개발자 R3 #3 — 엔진이 _pct를 직접
        임포트하면 내부 반올림 정책 변경에 조용히 깨진다)."""
        amount = price * quantity
        return amount + _pct(amount, COMMISSION_PCT)

    def reserved_buy_total(self) -> int:
        """미체결 매수 예약 합계(접수 시점 고정 reserve_price 기준 — §2).
        matching.submit의 가용현금 검사와 kt00001 ord_alow_amt 산출이
        같은 수식을 공유한다(실서버: 접수 시점 주문가능금액 차감)."""
        return sum(
            self.estimate_buy_cost(o.reserve_price, o.unfilled)
            for o in self.open_orders.values()
            if o.side == "buy" and o.unfilled > 0)

    # ── 체결 반영(매칭 엔진이 호출) ─────────────────────────────────────

    def apply_buy_fill(self, symbol: str, market: str, quantity: int,
                       price: int) -> None:
        amount = price * quantity
        # 수수료 수식은 commission/sell_tax 공개 함수로 단일화(개발자 R4 —
        # _pct 직접 호출이 남으면 요율 변경 시 한쪽만 고치는 사고 가능)
        fee = commission(amount)
        self.cash -= amount + fee
        if self.cash < 0:
            # 접수 시점 예약 차감(matching.submit — 실서버 ord_alow_amt 재현)
            # 이 1차 방어선이지만, 시장가 미체결의 예약액은 현재가 근사라
            # 순차 체결이 검사를 초과할 수 있다(스펙 §2 잔여 한계).
            # 침묵 음수 금지 — 카운터+경고로 표면화(kt00001 원천 오염 감지).
            self.negative_cash_events += 1
            logger.warning("cash went negative (%d) after buy fill %s — "
                           "reservation approximation limit (spec §2)",
                           self.cash, symbol)
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
        fee = commission(amount)
        tax = sell_tax(amount, holding.market)
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
