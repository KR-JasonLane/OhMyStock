"""매칭 엔진(스펙 §8 — 단순 룰, 한계는 스펙 §2에 명시).

룰(§8):
- 시장가: 재생 시각 현재가(직전 분봉 close)로 즉시 전량 체결.
- 지정가 매수: `limit ≥ 현재가`면 즉시 체결(체결가=현재가 — 더 유리한 가격,
  실서버 관례), 아니면 미체결 등록 후 이후 재생 분봉 `low ≤ limit`이 되는
  첫 시점에 **limit 가격으로** 체결. 매도는 대칭(`high ≥ limit`).
- 마켓터블 재평가(트레이더 R3 #2 — §8 갱신): check 시점 현재가 기준 즉시
  체결 가능(buy: limit≥현재가 / sell: limit≤현재가)이면 **현재가로** 체결.
  과거 구간 크로스(스치고 복귀)만 limit가 체결.
- **크로스 판정은 replay_now 진행과 동기**(§5/§8): 등록 시점에 미래 분봉을
  스캔해 체결 시각을 예약하는 구현 금지 — check_fills(now)가 (직전 검사
  시각, now] 구간만 순차 검사한다(candles_between의 until 상한 계약).
- 전파 지연(§8 기본 재현): 접수 후 FaultPolicy.propagation_delay_sec(벽시계)
  동안 ka10075에 미노출(account.visible_after).
- 결함 훅은 FaultPolicy로만(§9 seam) — 이 모듈에 시나리오 상태 없음.

검증(§7 실측 재현):
- 지정가 틱 위반 → RC4003 계열 거부(is_on_tick — 판별 실측).
- 매수 예수금 부족/매도 보유 부족 → 거부. 매수는 **미체결 매수 예약분을
  차감한 가용현금**으로 검사(트레이더 R3 #1 — 실서버 ord_alow_amt 차감
  재현). 예약 단가는 접수 시점 고정(OpenOrder.reserve_price). 잔여 한계:
  시장가 미체결의 예약액은 접수 시점 현재가 근사(스펙 §2).

체결가 한계(§2): 분봉 close/limit 단순가 — 슬리피지·호가 소진 미재현."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from replay.account import Account, OpenOrder
from replay.faults import FaultPolicy
from replay.minute_store import MinuteStore
from replay.ticks import is_on_tick

logger = logging.getLogger(__name__)


def _is_marketable(side: str, limit_price: int, current_price: int) -> bool:
    """§8 마켓터블 판정 — 현재가 기준 즉시 체결 가능(buy: limit≥현재가 /
    sell: limit≤현재가). submit 즉시체결과 check_fills 재평가가 같은 규칙
    정의를 공유한다(개발자 R3 델타 #1 — 조건식 중복 제거: 규칙이 바뀔 때
    한쪽만 고쳐지는 누락 방지)."""
    if side == "buy":
        return limit_price >= current_price
    return limit_price <= current_price


@dataclass(frozen=True)
class OrderResult:
    ok: bool
    order_no: str = ""
    reason: str = ""       # 거부 시 rc 메시지 성격(api 계층이 return_msg로)


class MatchingEngine:
    def __init__(self, account: Account, store: MinuteStore,
                 replay_now: Callable[[], datetime],
                 wall_now: Callable[[], datetime],
                 faults: FaultPolicy | None = None,
                 default_market: str = "kospi") -> None:
        """replay_now: () -> KST-aware 재생 시각(ReplayClock.now).
        wall_now: () -> 벽시계 datetime(전파 지연 판정 — 배속 무관 §5).
        default_market: 심볼→시장 매핑이 없는 목의 단순화(§2) — 세금/틱은
        기본 kospi, ETF 심볼은 api 계층이 명시 전달."""
        self._account = account
        self._store = store
        # 미래 누출 클램프 자동 바인딩(아키텍트 R3 델타 — candles_between의
        # until 클램프가 R4 배선의 기억에 의존하지 않게 구조로). 오프라인
        # 게이트는 엔진 없이 별도 MinuteStore를 쓰므로 충돌 없음.
        if store.now_provider is not None:
            # 사용 규약 위반 신호(개발자 R3 델타 Minor — 같은 store를 다른
            # 엔진/게이트와 공유하면 시계가 조용히 뒤바뀐다): 침묵 금지.
            logger.warning("MinuteStore.now_provider already bound — "
                           "overwriting (store shared across engines?)")
        store.now_provider = replay_now
        self._replay_now = replay_now
        self._wall_now = wall_now
        self._faults = faults or FaultPolicy()
        self._default_market = default_market
        # 미체결별 (마지막 크로스 검사 재생 시각, market) — replay_now 동기
        self._watch: dict[str, tuple[datetime, str]] = {}
        # 시세 부재로 판정 스킵된 횟수(침묵 금지 관례 — 보안 R3 Minor:
        # 심볼 필터 실수로 주문이 영원히 잔존해도 신호가 남게)
        self.price_missing_skips = 0

    # ── 조회 ────────────────────────────────────────────────────────────

    def price_now(self, symbol: str) -> int | None:
        candle = self._store.last_at_or_before(symbol, self._replay_now())
        return candle.close if candle else None

    # ── 접수 ────────────────────────────────────────────────────────────

    def submit(self, symbol: str, side: str, style: str, quantity: int,
               limit_price: int = 0, market: str | None = None) -> OrderResult:
        if market is not None and market not in ("kospi", "kosdaq", "etf"):
            return OrderResult(False, reason=f"unknown market {market!r}")
        if side == "sell" and symbol in self._account.holdings:
            # 매도는 보유 포지션의 실제 시장이 ground truth(아키텍트 R3 #4 —
            # API 계층 재전달 누락 시 ETF 틱/세율 오적용 방지). 파라미터/
            # 기본값은 신규 매수에만.
            market = self._account.holdings[symbol].market
        else:
            market = market or self._default_market
        reject = self._faults.reject_order(symbol)
        if reject is not None:
            return OrderResult(False, reason=reject)
        if quantity <= 0:
            return OrderResult(False, reason="invalid quantity")
        price = self.price_now(symbol)
        if price is None:
            return OrderResult(False, reason="no market data for symbol")
        if style == "limit":
            if limit_price <= 0:
                return OrderResult(False, reason="limit requires price")
            if not is_on_tick(limit_price, market):
                # RC4003 재현(틱 판별 실측 — CLAUDE.md §5)
                return OrderResult(
                    False, reason="[2000](RC4003:모의투자 호가단위 오류입니다.)")
        if side == "buy":
            # 미체결 매수 예약 반영 가용현금(트레이더 R3 #1 — 실서버는 접수
            # 시점에 ord_alow_amt를 차감한다: 같은 자금으로 두 번째 매수를
            # 내면 거부. 예약 없이 통과시키면 중복 진입 버그가 리플레이에서
            # 정상처럼 보인다). 예약 단가는 접수 시점 고정(reserve_price —
            # 이후 시세 결측이 예약액을 침묵 0원으로 만들 수 없다).
            reserved = sum(
                self._account.estimate_buy_cost(o.reserve_price, o.unfilled)
                for o in self._account.open_orders.values()
                if o.side == "buy" and o.unfilled > 0)
            cost = self._account.estimate_buy_cost(limit_price or price,
                                                   quantity)
            if self._account.cash - reserved < cost:
                return OrderResult(False, reason="insufficient cash")
        else:
            holding = self._account.holdings.get(symbol)
            held = holding.quantity if holding else 0
            pending_sells = sum(
                o.unfilled for o in self._account.open_orders.values()
                if o.symbol == symbol and o.side == "sell")
            if held - pending_sells < quantity:
                return OrderResult(False, reason="insufficient holdings")

        order_no = self._account.next_order_no()
        wall = self._wall_now()
        order = OpenOrder(
            order_no=order_no, symbol=symbol, side=side, style=style,
            quantity=quantity, unfilled=quantity,
            price=limit_price if style == "limit" else 0,
            reserve_price=(limit_price or price) if side == "buy" else 0,
            submitted_at=wall,
            visible_after=wall + timedelta(
                seconds=self._faults.propagation_delay_sec()))
        self._account.open_orders[order_no] = order
        self._watch[order_no] = (self._replay_now(), market)

        # 즉시 체결 판정(시장가 / 마켓터블 지정가)
        if style == "market" or _is_marketable(side, limit_price, price):
            self._fill(order, price, market)  # 즉시 체결 — 체결가=현재가
        return OrderResult(True, order_no=order_no)

    # ── 진행(크로스 판정 — replay_now 동기) ─────────────────────────────

    def check_fills(self) -> None:
        """미체결 지정가의 크로스 판정 — (직전 검사, now] 분봉만 검사(§5).
        시장가 잔존(체결 억제 시나리오)은 크로스 개념이 없어 억제 해제 후
        다음 검사에서 현재가로 체결."""
        now = self._replay_now()
        for order_no, order in list(self._account.open_orders.items()):
            if order.unfilled <= 0:
                continue
            if self._faults.suppress_fill(order_no):
                continue
            # 미등록 watch는 fail-loud(개발자 R3 #2 — 침묵 기본값은 스캔
            # 구간 유실+market 오분류를 조용히 만든다. submit이 항상 등록)
            last_checked, market = self._watch[order_no]
            if order.style == "market":
                price = self.price_now(order.symbol)
                if price is None:
                    self.price_missing_skips += 1  # 침묵 스킵 금지 — 카운터
                else:
                    self._fill(order, price, market)
                continue
            # 마켓터블 재평가(트레이더 R3 #2 — 억제 해제/스프레드 크로스
            # 재개): 현재가 기준 즉시 체결 가능하면 **현재가로** 체결(스탠딩
            # 지정가가 스프레드를 넘는 도착·재개 시의 실서버 관례 — §8 갱신).
            price = self.price_now(order.symbol)
            if price is not None and _is_marketable(order.side, order.price,
                                                    price):
                self._fill(order, price, market)
            # 부분체결(FaultPolicy 훅)이어도 구간 내 이후 캔들을 계속 검사 —
            # break로 끊고 watch=now로 점프하면 이미 관측된 크로스 기회가
            # 영구 유실된다(개발자 R3 Critical #1). 과거 구간 크로스(복귀한
            # 가격)는 스탠딩 주문의 limit 가격 체결.
            if order.unfilled > 0:
                for candle in self._store.candles_between(order.symbol,
                                                          last_checked, now):
                    if order.unfilled <= 0:
                        break
                    crossed = (candle.low <= order.price
                               if order.side == "buy"
                               else candle.high >= order.price)
                    if crossed:
                        self._fill(order, order.price, market)  # 체결가=limit
            if order.unfilled > 0:
                self._watch[order_no] = (now, market)

    # ── 취소 ────────────────────────────────────────────────────────────

    def cancel(self, order_no: str) -> OrderResult:
        order = self._account.open_orders.get(order_no)
        if order is None or order.unfilled <= 0:
            # 체결 완료/미지 주문 취소 — 거부(가정: 미실측, 스펙 §7 PRE-GATE).
            # 존재 확인을 결함 훅보다 먼저(트레이더 Minor — "주문 없음"과
            # "브로커 취소 거부"를 시나리오 로그에서 구분 가능하게)
            return OrderResult(False, reason="order not open")
        reject = self._faults.reject_cancel(order_no)
        if reject is not None:
            return OrderResult(False, reason=reject)
        del self._account.open_orders[order_no]
        self._watch.pop(order_no, None)
        return OrderResult(True, order_no=order_no)

    # ── 내부 ────────────────────────────────────────────────────────────

    def _fill(self, order: OpenOrder, price: int, market: str) -> None:
        # suppress 검사: submit 즉시체결 경로에서는 이 지점이 **유일한**
        # 방어선(check_fills 경로에서는 상위 검사와 의도적 이중 — 개발자 Minor)
        if self._faults.suppress_fill(order.order_no):
            return
        quantity = self._faults.fill_quantity(order.order_no, order.unfilled)
        if quantity <= 0:
            return
        quantity = min(quantity, order.unfilled)
        if order.side == "buy":
            self._account.apply_buy_fill(order.symbol, market, quantity, price)
        else:
            self._account.apply_sell_fill(order.symbol, quantity, price)
        order.unfilled -= quantity
        if order.unfilled == 0:
            self._account.open_orders.pop(order.order_no, None)
            self._watch.pop(order.order_no, None)
