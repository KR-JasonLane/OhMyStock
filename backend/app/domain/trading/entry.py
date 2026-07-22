"""진입 집행(EntryExecutor) — 지정가 발주 → 미체결 감시 → 시장가 폴백(스펙
§6-1/§6-3.6-8). 상태 전이는 EntryPhase enum 룩업(if 중첩 금지 — 계획서 6a).

부수효과 경계: OrderPort(주문)·콜백 2개만 안다 — store/adapters 임포트 금지
(계획서 Global Constraints). 상태 영속(persist_phase — §6-6 재기동 복구의 전제:
EntryPhase가 DB에 있어야 고아 취소를 식별)과 주문 감사 기록(on_order)은 Task 7
TradingService가 TradingStore를 연결해 주입한다.

check_order_caps(amount_krw)는 **place_order 호출 직전** 실행된다(§8-1 단건
상한 발주 직전 재검증 — 보안 패널: 누적 상한은 "다음 주문 전"에 걸리므로
사이징 버그의 첫 주문을 이 훅만이 잡는다). 구현체는 Task 7이 주입.

체결가 주의: 반환 TradePosition.entry_price는 **추정치**(지정가=발주가,
시장가=관측 ask)다 — mock 시장가 체결가는 호가 그리드 비준수(블렌디드,
CLAUDE.md §5)라 정확한 평단은 Task 7이 잔고(kt00018 pur_pric — G3 실측 원 단위
정수)로 확정한다(costs.py의 "실측 필드 우선" 원칙과 동일)."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from app.domain.broker import (OrderAck, OrderPort, OrderRequest, OrderSide,
                               OrderStyle)
from app.domain.trading.config import TradingConfig
from app.domain.trading.models import EntryPhase, PositionState, TradePosition
from app.domain.trading.selection import EntryPlan
from app.domain.trading.ticks import round_to_tick, tick_size

logger = logging.getLogger(__name__)

# 주문 감사 콜백 — (ack, request, status) → Task 7이 store.record_order 연결.
# **예외 격리**(보안 패널): 주문이 이미 브로커에 나간 뒤 호출되므로 기록 실패가
# 하류(폴링·취소·포지션 추적)를 중단시키면 "주문은 나갔는데 추적이 끊기는"
# 최악 상태가 된다 — _audit()이 예외를 삼키고 error 로그만 남긴다.
OnOrder = Callable[[OrderAck, OrderRequest, str], None]
# 상태 영속 콜백 — Task 7이 store.update_position(entry_phase=...) 연결.
# **의도적 비격리(fail-closed)**: 항상 발주 *이전*에 호출되므로 영속 실패 시
# 주문이 아예 나가지 않는 안전한 방향 — 예외를 그대로 전파한다.
PersistPhase = Callable[[EntryPhase], None]


@dataclass(frozen=True)
class EntryOutcome:
    """execute 결과. position=None이면 ENTRY_FAILED(진입 실패 — 0체결)이며
    사유는 failure_reason. 부분 체결은 position.quantity < plan.quantity.

    requires_reconcile: 체결 여부 불명(시장가 관측 전무·취소 실패) — §6-6
    reconcile(잔고 대조)이 실상을 복구해야 한다. Task 7은 이 **구조화 필드**로
    분기한다 — failure_reason 문자열 매칭 금지(개발자 패널: 메시지 문구가
    바뀌면 조용히 깨지는 안티패턴).

    ⚠️ requires_reconcile=True인 position=None은 확정 ENTRY_FAILED가 **아니다**
    (아키텍트 패널 #1): ENTRY_FAILED는 §6-6 재기동 스캔 집합 밖이라, 미니
    reconcile 완료 전에 ENTRY_FAILED로 영속하면 실보유가 어떤 재기동 스윕에도
    다시 걸리지 않고 손절 감시 밖에 영구 방치된다. Task 7은 마지막으로
    persist_phase된 EntryPhase(CANCEL_REQUESTED/MARKET_SUBMITTED)를 유지한 채
    미니 reconcile(잔고 대사)이 최종 상태를 결정하게 해야 한다."""
    position: TradePosition | None
    failure_reason: str | None = None
    requires_reconcile: bool = False


class EntryExecutor:
    def __init__(self, orders: OrderPort, config: TradingConfig,
                 check_order_caps: Callable[[int], None],
                 persist_phase: PersistPhase | None = None,
                 on_order: OnOrder | None = None,
                 sleep: Callable[[float], Awaitable[None]] | None = None,
                 now: Callable[[], datetime] | None = None) -> None:
        self._orders = orders
        self._config = config
        self._check_caps = check_order_caps
        self._persist = persist_phase or (lambda _phase: None)
        self._on_order = on_order or (lambda *_args: None)
        self._sleep = sleep or asyncio.sleep
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _audit(self, ack: OrderAck, req: OrderRequest, status: str) -> None:
        """on_order 예외 격리 — 주문은 이미 나갔으므로 기록 실패가 흐름을
        죽이면 안 된다(보안 패널 #3). 실패는 error 로그로 표면화(주문번호 포함
        — 수동 감사 복구 가능)."""
        try:
            self._on_order(ack, req, status)
        except Exception as exc:  # noqa: BLE001
            logger.error("order audit callback failed for %s (order_no=%s, "
                         "status=%s): %s — manual audit reconstruction needed",
                         req.symbol, ack.order_no, status, exc)

    async def _submit(self, phase: EntryPhase, req: OrderRequest) -> OrderAck:
        """영속(fail-closed, 발주 전) → 발주 → 감사 — 3단 쌍을 한 곳에(개발자
        패널: 손 중복이 곧 감사 누락의 원인). 취소는 경로별 실패 의미가 달라
        (지정가 취소 실패=폴백 중단, 잔여 취소 실패=사유 보존) 묶지 않는다."""
        self._persist(phase)
        ack = await self._orders.place_order(req)
        self._audit(ack, req, "submitted")
        return ack

    async def _refresh_ask(self, symbol: str, stale_ask: int) -> int:
        """시장가 재발주 직전 시세 재조회(트레이더 패널 I1). stale_ask는
        limit_order_timeout_sec(기본 60s) 이전 관측치 — 지정가가 안 잡혔다는
        것 자체가 가격 급변 신호라, 바로 그 구간에서 caps 재검증·평단 추정이
        가장 낡은 데이터로 돌면 안 된다(ka10095는 주문과 분리 버킷 — 비용
        미미). 재조회 실패 시 stale 폴백 + 경고(발주 자체는 막지 않음 —
        미체결 방치가 더 큰 리스크, §6-1)."""
        try:
            quotes = await self._orders.get_quotes([symbol])
            if quotes and quotes[0].ask > 0:
                return quotes[0].ask
            logger.warning("requote for %s returned no usable ask — "
                           "using stale ask %d", symbol, stale_ask)
        except Exception as exc:  # noqa: BLE001
            logger.warning("requote failed for %s (%s) — using stale ask %d",
                           symbol, exc, stale_ask)
        return stale_ask

    async def execute(self, plan: EntryPlan, ask: int) -> EntryOutcome:
        """plan을 집행한다. ask는 호출자(Task 7)가 get_quotes로 확보한 최우선
        매도호가 — 지정가 산정에는 이 값을 그대로 쓴다(호출 직후라 신선,
        재조회는 버킷만 소모). 단 시장가 폴백 직전에는 _refresh_ask로
        재조회한다(타임아웃 동안 낡음 — 트레이더 I1).

        흐름(§6-3.6-8): 지정가(ask−offset틱, 틱 스냅) 발주[LIMIT_SUBMITTED] →
        limit_order_timeout_sec 동안 체결 폴링 → 전량 미체결이면 취소
        [CANCEL_REQUESTED] 후 시장가 재발주[MARKET_SUBMITTED] → 체결 확인.
        부분 체결은 체결분만 인정 + 잔량 취소(시장가 재발주 없음 — §6-1)."""
        if ask <= 0:
            return EntryOutcome(None, f"invalid ask for {plan.symbol}: {ask}")
        limit_price = self._limit_price(ask, plan.market)

        # [1] 지정가 발주 — 단건 상한은 발주 직전 재검증(§8-1)
        self._check_caps(limit_price * plan.quantity)
        limit_req = OrderRequest(symbol=plan.symbol, side=OrderSide.BUY,
                                 style=OrderStyle.LIMIT,
                                 quantity=plan.quantity,
                                 limit_price=limit_price)
        ack = await self._submit(EntryPhase.LIMIT_SUBMITTED, limit_req)

        # [2] 체결 폴링(타임아웃까지). unfilled<0 = 관측 전무(조회 계속 실패) —
        # 전량 미체결로 보수 간주(취소 경로가 실주문을 정리, 체결분은 §6-6
        # reconcile이 잔고 ground truth로 복구)
        unfilled = await self._poll_unfilled(ack.order_no,
                                             self._config.limit_order_timeout_sec)
        if unfilled == 0:
            return EntryOutcome(self._position(plan, limit_price))
        if unfilled < 0:
            unfilled = plan.quantity

        # [3] 타임아웃 — 취소(부분 체결이면 잔량 취소가 곧 §6-1 계약).
        # 취소 실패 = 주문 상태 불명(마지막 폴 이후 체결됐을 수도 — 트레이더
        # C1/I4). 이때 시장가를 재발주하면 이중 매수가 되므로 폴백을 중단하고
        # reconcile(잔고 ground truth)로 위임한다.
        self._persist(EntryPhase.CANCEL_REQUESTED)
        try:
            cancel_ack = await self._orders.cancel_order(ack.order_no,
                                                         plan.symbol)
        except Exception as exc:  # noqa: BLE001 — 이중 매수 가드가 우선
            logger.error("entry limit cancel FAILED %s (order_no=%s): %s — "
                         "market fallback skipped (double-buy guard), "
                         "reconcile required", plan.symbol, ack.order_no, exc)
            return EntryOutcome(
                None, f"limit cancel failed for {plan.symbol} "
                      f"(order_no={ack.order_no}) — order state unknown, "
                      f"market fallback skipped", requires_reconcile=True)
        self._audit(cancel_ack, limit_req, "cancelled")

        filled = plan.quantity - unfilled
        if filled > 0:
            # 부분 체결 — 체결분만 포지션 인정, 시장가 재발주 없음(§6-1:
            # 전량 대기하면 이미 보유한 수량이 손절 감시 밖에 놓인다).
            # 확정 수량은 취소 직전 마지막 폴 스냅샷(최대 1 interval 낡음) —
            # Task 7이 진입 직후 잔고 대사로 재확정한다(트레이더 I4).
            logger.info("entry partial fill %s: %d/%d — remainder cancelled",
                        plan.symbol, filled, plan.quantity)
            return EntryOutcome(self._position(plan, limit_price, quantity=filled))

        # [4] 0체결 — 시장가 재발주(§6-3.8). ask는 타임아웃 동안 낡았고
        # 미체결 자체가 급변 신호 — caps·평단 추정 모두 재조회 값으로(I1).
        fresh_ask = await self._refresh_ask(plan.symbol, ask)
        self._check_caps(fresh_ask * plan.quantity)
        market_req = OrderRequest(symbol=plan.symbol, side=OrderSide.BUY,
                                  style=OrderStyle.MARKET,
                                  quantity=plan.quantity)
        market_ack = await self._submit(EntryPhase.MARKET_SUBMITTED, market_req)

        unfilled = await self._poll_unfilled(market_ack.order_no,
                                             self._config.limit_order_timeout_sec)
        if unfilled < 0:
            # 시장가 후 관측 전무 — 체결 여부 불명. 포지션을 만들지도(허위
            # ENTERED), 버리지도(감시 밖 방치) 않고 실패로 표면화 —
            # requires_reconcile 표식으로 Task 7이 **즉시** 잔고 대사를
            # 트리거한다(재기동 대기 금지 — 트레이더 I2).
            return EntryOutcome(
                None, f"market order unobservable for {plan.symbol} "
                      f"(order_no={market_ack.order_no}) — reconcile required",
                requires_reconcile=True)
        filled = plan.quantity - unfilled
        cancel_failed = False
        if unfilled > 0:
            # 잔여 취소 — 0체결이든 부분체결이든 잔량을 시장에 방치하지
            # 않는다(감시 밖 지연 체결 = 미추적 수량 — 개발자 #3/트레이더 I3)
            try:
                residual_ack = await self._orders.cancel_order(
                    market_ack.order_no, plan.symbol)
                self._audit(residual_ack, market_req, "cancelled")  # 보안 #1
            except Exception as exc:  # noqa: BLE001 — 실패해도 사유 보존이 우선
                # 미추적 라이브 주문이 남을 수 있는 상황 — order_no 포함 error
                # (수동 취소 대상 식별 가능, 보안 #2) + reconcile 표식(I3).
                cancel_failed = True
                logger.error("entry market-order cancel FAILED %s "
                             "(order_no=%s): %s — live order may remain, "
                             "reconcile/manual cancel required",
                             plan.symbol, market_ack.order_no, exc)
        if filled <= 0:
            # 시장가조차 0체결(거래정지 급전환 등 비정상) — ENTRY_FAILED.
            reason = (f"market order unfilled for {plan.symbol} "
                      f"(order_no={market_ack.order_no})")
            if cancel_failed:
                reason += "; residual cancel FAILED — order may still be live"
            return EntryOutcome(None, reason, requires_reconcile=cancel_failed)
        if filled < plan.quantity:
            logger.info("entry partial fill (market) %s: %d/%d — remainder "
                        "cancelled", plan.symbol, filled, plan.quantity)
        return EntryOutcome(self._position(plan, fresh_ask, quantity=filled),
                            requires_reconcile=cancel_failed)

    def _limit_price(self, ask: int, market: str) -> int:
        """진입 지정가 = ask − offset틱, 호가단위 스냅(§6-3.6)."""
        offset = self._config.entry_tick_offset
        price = ask - offset * tick_size(ask, market)
        return round_to_tick(max(price, 1), market, "down")

    async def _poll_unfilled(self, order_no: str, timeout_sec: float) -> int:
        """타임아웃까지 미체결 잔량 폴링. 반환: 0=전량 체결 확정, 양수=미체결
        잔량(마지막 관측), -1=관측 전무(체결 여부 미확정 — 호출자가 보수 처리).

        주문 부재(get_open_orders에 없음)는 '체결'과 '미체결 시스템 미전파'를
        구분하지 못한다(트레이더 C1 — 접수 TR(ordr)과 조회 TR(acnt)은 다른
        백엔드 계층, 전파 지연 가능). 오판 방어 3중:
        [a] 발주 직후 첫 폴 전 1 interval 전파 유예
        [b] 존재를 한 번도 관측 못 한 주문의 부재는 **성공 조회 연속 2회**에서
            확인된 뒤에만 체결로 판정. 한 번이라도 관측된 주문의 부재는 즉시
            체결(우리가 취소하지 않았으므로 등록된 주문의 소멸 원인은 체결뿐)
        [c] 잔여 리스크(전파 지연 > 유예+확인 창)는 Task 7이 진입 직후 잔고
            대사(kt00018)로 수량·평단을 확정하며 봉쇄(유령 포지션 즉시 해소)
        조회 실패(BrokerError)는 부재 관측이 아니다 — 연속 부재 카운트를
        리셋하고 재시도. 데드라인은 근사치 — 마지막 반복이 최대 interval만큼
        초과할 수 있다(보수 방향, 개발자 Minor)."""
        deadline = timeout_sec
        interval = min(self._config.poll_interval_sec, timeout_sec)
        last_unfilled: int | None = None
        seen = False          # 이 주문이 조회 시스템에 등록된 것을 관측했는가
        absent_streak = 0     # 성공 조회 기준 연속 부재 횟수
        elapsed = 0.0
        await self._sleep(interval)  # [a] 전파 유예
        elapsed += interval
        while True:
            try:
                open_orders = await self._orders.get_open_orders()
                mine = [o for o in open_orders if o.order_no == order_no]
                if mine:
                    seen = True
                    absent_streak = 0
                    last_unfilled = mine[0].unfilled_qty
                    if last_unfilled == 0:
                        return 0
                else:
                    absent_streak += 1
                    if seen or absent_streak >= 2:  # [b]
                        return 0
            except Exception as exc:  # noqa: BLE001 — 조회 실패는 재시도
                absent_streak = 0  # 실패는 부재 '관측'이 아니다
                logger.warning("open-order poll failed for order_no=%s (%s) — "
                               "retrying", order_no, exc)
            if elapsed >= deadline:
                # 타임아웃 — 마지막 관측 잔량(관측 전무면 -1: 호출자가 보수
                # 처리 — 지정가는 취소 시도, 시장가는 reconcile 표식)
                return last_unfilled if last_unfilled is not None else -1
            await self._sleep(interval)
            elapsed += interval

    def _position(self, plan: EntryPlan, entry_price: int,
                  quantity: int | None = None) -> TradePosition:
        qty = quantity if quantity is not None else plan.quantity
        return TradePosition(
            symbol=plan.symbol, name=plan.name, market=plan.market,
            state=PositionState.ENTERED, entry_price=entry_price,
            quantity=qty, peak_price=entry_price, trailing_active=False,
            entered_at=self._now())
