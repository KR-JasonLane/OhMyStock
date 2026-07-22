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
# 발주 트리오·감사 격리·C1-안전 폴링은 execution.py 공용 헬퍼(6a/6b 공유 —
# 아키텍트 P5-T6a #2). OnOrder 계약 정의도 그쪽이 소유(여기서 재수출).
from app.domain.trading.execution import (OnOrder, audit_order,  # noqa: F401
                                          poll_unfilled, submit_order)
from app.domain.trading.models import EntryPhase, PositionState, TradePosition
from app.domain.trading.selection import EntryPlan
from app.domain.trading.ticks import round_to_tick, tick_size

logger = logging.getLogger(__name__)

# 상태 영속 콜백 — Task 7이 store.update_position(entry_phase=...) 연결.
# **의도적 비격리(fail-closed)**: 항상 발주 *이전*에 호출되므로 영속 실패 시
# 주문이 아예 나가지 않는 안전한 방향 — 예외를 그대로 전파한다.
# (on_order 감사 콜백의 격리 계약은 execution.audit_order 참조.)
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
                 check_order_caps: Callable[[int, OrderSide], None],
                 persist_phase: PersistPhase | None = None,
                 on_order: OnOrder | None = None,
                 sleep: Callable[[float], Awaitable[None]] | None = None,
                 now: Callable[[], datetime] | None = None) -> None:
        self._orders = orders
        self._config = config
        self._check_caps = check_order_caps
        self._persist = persist_phase or (lambda _phase: None)
        self._on_order = on_order
        self._sleep = sleep or asyncio.sleep
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _audit(self, ack: OrderAck, req: OrderRequest, status: str) -> None:
        """execution.audit_order 위임 — 격리 계약(보안 #3)은 그쪽 docstring."""
        audit_order(self._on_order, ack, req, status)

    async def _submit(self, phase: EntryPhase, req: OrderRequest) -> OrderAck:
        """execution.submit_order 위임 — persist(fail-closed)→발주→감사 트리오."""
        return await submit_order(self._orders, req,
                                  persist=lambda: self._persist(phase),
                                  on_order=self._on_order)

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

        # [1] 지정가 발주 — 단건 상한은 발주 직전 재검증(§8-1). side 전달은
        # P5-T6b 트레이더 C2: 상한은 매수 봉쇄용 — 구현체(Task 7)가 매도
        # (청산·킬스위치)를 차단하지 않으려면 방향을 알아야 한다.
        self._check_caps(limit_price * plan.quantity, OrderSide.BUY)
        limit_req = OrderRequest(symbol=plan.symbol, side=OrderSide.BUY,
                                 style=OrderStyle.LIMIT,
                                 quantity=plan.quantity,
                                 limit_price=limit_price)
        ack = await self._submit(EntryPhase.LIMIT_SUBMITTED, limit_req)
        return await self._settle_limit(plan, ask, ack.order_no, limit_req)

    async def resume(self, plan: EntryPlan, ask: int, order_no: str,
                     limit_price: int) -> EntryOutcome:
        """재기동 reconcile ②(진입 지정가 생존, 창 안) 재개 — 신규 발주 없이
        기존 주문의 폴링→취소→시장가 폴백 꼬리를 이어받는다(아키텍트 P5-T6c
        #3: Task 7이 이 꼬리를 재구현하면 6a 로직과 드리프트한다).
        limit_price는 생존 주문의 발주가(ka10075 ord_pric — 부분 체결 인정 시
        진입가 추정에 사용), ask는 재기동 후 재조회한 시세."""
        if ask <= 0:
            return EntryOutcome(None, f"invalid ask for {plan.symbol}: {ask}")
        limit_req = OrderRequest(symbol=plan.symbol, side=OrderSide.BUY,
                                 style=OrderStyle.LIMIT,
                                 quantity=plan.quantity,
                                 limit_price=limit_price)
        return await self._settle_limit(plan, ask, order_no, limit_req)

    async def _settle_limit(self, plan: EntryPlan, ask: int, order_no: str,
                            limit_req: OrderRequest) -> EntryOutcome:
        """지정가 발주 이후의 공통 꼬리([2] 폴링 → [3] 취소 → [4] 시장가
        폴백). execute(신규 발주)와 resume(재기동 재개)이 공유한다."""
        limit_price = limit_req.limit_price
        # [2] 체결 폴링(타임아웃까지). unfilled<0 = 관측 전무(조회 계속 실패) —
        # 전량 미체결로 보수 간주(취소 경로가 실주문을 정리, 체결분은 §6-6
        # reconcile이 잔고 ground truth로 복구)
        unfilled = await self._poll_unfilled(order_no,
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
            cancel_ack = await self._orders.cancel_order(order_no,
                                                         plan.symbol)
        except Exception as exc:  # noqa: BLE001 — 이중 매수 가드가 우선
            logger.error("entry limit cancel FAILED %s (order_no=%s): %s — "
                         "market fallback skipped (double-buy guard), "
                         "reconcile required", plan.symbol, order_no, exc)
            return EntryOutcome(
                None, f"limit cancel failed for {plan.symbol} "
                      f"(order_no={order_no}) — order state unknown, "
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
        self._check_caps(fresh_ask * plan.quantity, OrderSide.BUY)
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
        """execution.poll_unfilled 위임 — C1 3중 방어(전파 유예/연속 2회 부재
        확인/Task 7 잔고 대사) 계약은 그쪽 docstring."""
        return await poll_unfilled(
            self._orders, order_no, timeout_sec=timeout_sec,
            interval_sec=self._config.poll_interval_sec, sleep=self._sleep)

    def _position(self, plan: EntryPlan, entry_price: int,
                  quantity: int | None = None) -> TradePosition:
        qty = quantity if quantity is not None else plan.quantity
        return TradePosition(
            symbol=plan.symbol, name=plan.name, market=plan.market,
            state=PositionState.ENTERED, entry_price=entry_price,
            quantity=qty, peak_price=entry_price, trailing_active=False,
            entered_at=self._now())
