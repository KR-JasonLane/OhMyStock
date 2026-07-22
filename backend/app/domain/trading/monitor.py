"""감시 루프(PositionMonitor) — 폴링 1사이클: 다종목 시세 1회 조회 →
순수 청산 판정(exit_rules) → §6-2-b 청산 집행 → 실현손익(costs) 기록.

계층 경계(계획서 Task 6b·아키텍트 P5-T6a #2): OrderPort + 콜백 2개
(persist_position fail-closed / on_order 격리)만 의존 — store/adapters 임포트
금지. Task 7 TradingService가 TradingStore·계측을 연결해 주입한다. 판정은
exit_rules.evaluate_exit(순수)가 전담하고 여기는 **저장과 집행만** 한다.

핵심 계약:
- **trailing_active/peak는 입력 TradePosition의 값(=DB 영속값, 직전 관측
  상태)을 그대로 evaluate_exit에 전달**한다 — 재계산 금지(P5-T3 트레이더
  이월: 재계산하면 익절 백스톱이 통합 지점에서 도달 불가). 매 관측 결과는
  주문 여부와 무관하게 즉시 영속(§6-2 — 재시작 시 래치·고점 리셋 방지).
- **조회 실패 ≠ 가격 불변**(§6-4): 실패 사이클은 판정을 건너뛰고 카운터만
  올린다. 연속 실패 quote_failure_threshold 초과 시 경고 상태(warnings에
  노출)로 전환하되 폴링은 계속한다. 특정 종목만 계속 결측이면
  lookup_instrument_state로 거래정지(예상된 실패) vs 네트워크 이상을 구분.
- **매도 주문은 성급히 취소하지 않는다**: 동시호가(15:20~15:30 KST)·VI
  구간은 "폴링 후 즉시 체결" 가정이 깨진다(§6-4). 체결 확인 타임아웃이
  지나도 주문을 살려둔 채 _pending으로 추적하고 다음 사이클에서 재확인 —
  하방 청산은 슬리피지보다 미청산 리스크가 크다(§6-2-b). 예외는 익절
  지정가의 시장가 폴백 전 취소뿐(§6-2-b 표의 명시 폴백).
- 청산 체결가/실현손익은 관측 시세 기반 **추정치** — Task 7이 잔고(kt00018)
  대사로 확정한다(6a entry_price와 동일 원칙).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, time, timezone

from app.domain.broker import (MarketData, OrderAck, OrderPort, OrderRequest,
                               OrderSide, OrderStyle)
from app.domain.trading.config import TradingConfig
from app.domain.trading.costs import realized_pnl
from app.domain.trading.exit_rules import evaluate_exit
from app.domain.trading.execution import (OnOrder, PersistPosition,
                                          audit_order, poll_unfilled,
                                          submit_order)
from app.domain.trading.models import (ExitPhase, ExitReason, PositionState,
                                       TradePosition)
from app.domain.trading.ticks import round_to_tick

logger = logging.getLogger(__name__)

# PersistPosition 계약(정의는 execution.py — 6b/6c 공유): 발주 전 호출은
# fail-closed(예외 전파 — 주문이 안 나가는 안전 방향), **관측·체결 후 호출은
# 격리**(주문·체결은 이미 일어난 사실 — 기록 실패가 나머지 포지션 감시를
# 죽이면 안 된다. DB가 놓친 상태는 재기동 reconcile(§6-6 ④)이 잔고로 복구).
# 종목 상태 조회(거래정지 구분용) — Task 7이 list_instruments 기반으로 연결.
LookupState = Callable[[str], Awaitable[str | None]]

# 동시호가 진입 시각(15:20 KST) — 이후 주문은 15:30 단일가 매칭까지 대기(§6-4)
_AUCTION_START = time(15, 20)
_AUCTION_MATCH_GRACE_SEC = 30.0  # 15:30 매칭 후 체결 전파 여유
# 청산 발주 자체가 연속 실패하면 EXIT_FAILED로 고정(침묵 금지 — §6-1).
# 매 사이클 재시도가 무한 반복되며 경고만 쌓이는 것을 막는 상한.
_EXIT_SUBMIT_RETRY_LIMIT = 3

# 집행 우선순위 = ExitReason 선언 순(§6-2: 기간초과→손절→트레일링→익절)
_REASON_PRIORITY = {reason: i for i, reason in enumerate(ExitReason)}


@dataclass(frozen=True)
class ExitAction:
    """poll_once가 보고하는 청산 집행 결과 1건(Task 7 진행상태·로그용).
    state: CLOSED(청산 확정) | EXITING(주문 생존, _pending 추적 중) |
    EXIT_FAILED(발주 실패 고정·장마감 미체결) | ENTERED(집행 실패 — 다음
    사이클 재시도). requires_reconcile=True면 Task 7이 즉시 잔고 대사
    (§6-3.8 계약과 동일 — 문자열 매칭 분기 금지)."""
    symbol: str
    reason: ExitReason
    state: PositionState
    quantity: int
    exit_price: int | None = None   # 추정 체결가(관측 시세) — 잔고 대사로 확정
    realized_pnl: int | None = None
    detail: str = ""
    requires_reconcile: bool = False


@dataclass(frozen=True)
class _PendingExit:
    """체결 미확정 청산 주문(취소하지 않고 추적) — 다음 사이클에서 재확인.

    seen_alive: 이 주문이 미체결 조회(ka10075)에 등록된 것을 관측한 적이
    있는가. False(관측 전무 pending·reconcile 시드)면 부재=체결 판정에
    **연속 2회 부재 확인**을 요구한다(보안 P5-T6c #2 — 잘못된 시드/전파
    지연이 실보유를 CLOSED로 오판해 감시 밖 방치하는 것 방지, C1과 동일
    방어). 한 번이라도 관측되면 부재=체결 즉시 판정(6b 기존 계약)."""
    position: TradePosition   # EXITING으로 영속된 스냅샷
    order_no: str
    reason: ExitReason
    # 추정 체결가. None = 신뢰할 추정 없음(reconcile 시드) — 확정 시 pnl을
    # 계산하지 않고 None으로 영속(트레이더 P5-T6c I4: entry_price 따위를
    # 대입하면 손실이 0에 가깝게 과소평가된 "확정처럼 보이는" 숫자가 남는다).
    est_price: int | None
    seen_alive: bool = True
    absent_streak: int = 0


class PositionMonitor:
    """⚠️ 수명주기 계약(아키텍트 P5-T6b #5): **trade_run(거래일)당 새 인스턴스
    + 단일 루프에서 순차 호출**을 전제한다 — _pending/카운터/경고는 인메모리
    가변 상태라 인스턴스를 거래일 경계 너머 재사용하면 전일 잔류 _pending이
    다음 날 장전(is_market_hours=False)에 오판 EXIT_FAILED로 고정될 수 있다.
    Task 7 TradingService가 이 계약을 지켜 조립한다(재기동 복구는 6c reconcile
    소관 — DB의 EXITING 상태가 근거)."""

    def __init__(self, orders: OrderPort, config: TradingConfig, calendar,
                 check_order_caps: Callable[[int, OrderSide], None],
                 persist_position: PersistPosition,
                 on_order: OnOrder | None = None,
                 lookup_instrument_state: LookupState | None = None,
                 sleep: Callable[[float], Awaitable[None]] | None = None,
                 now: Callable[[], datetime] | None = None) -> None:
        """calendar: `held_business_days(entry_date, now)`·`is_market_hours
        (now)`·`KST`를 제공하는 객체(core.market_calendar 모듈 — Task 7 주입,
        테스트는 fake). domain이 core 모듈을 직접 임포트하지 않는 이유:
        공휴일 테이블 전역 상태에 결합하지 않고 시간 축을 주입 가능하게 유지."""
        self._orders = orders
        self._config = config
        self._calendar = calendar
        self._check_caps = check_order_caps
        self._persist = persist_position
        self._on_order = on_order
        self._lookup_state = lookup_instrument_state
        self._sleep = sleep or asyncio.sleep
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._pending: dict[str, _PendingExit] = {}
        self._quote_failures = 0                  # 전체 조회 연속 실패
        self._pending_check_failures = 0          # pending 재확인 연속 실패
        self._symbol_failures: dict[str, int] = {}  # 종목별 연속 결측
        self._submit_failures: dict[str, int] = {}  # 종목별 청산 발주 연속 실패
        # key→메시지 (상태 API 노출 — ⚠️ 예외 원문 금지, 타입명 등 정형 요약만.
        # 원문은 로그 전용: DB 예외는 DSN 자격증명 포함 가능 — 보안 P5-T6b #3)
        self._warnings: dict[str, str] = {}

    @property
    def warnings(self) -> list[str]:
        """경고 상태(§6-4 — 상태 API 노출용). 비면 정상."""
        return list(self._warnings.values())

    def recommended_delay(self, now: datetime | None = None) -> float:
        """다음 폴까지 권장 대기(초) — Task 7 루프 케이던스용. 동시호가
        구간·미확정 청산 추적 중에는 백오프(§6-4: 즉시 체결 가정 비적용
        구간에서 레이트리밋 낭비 방지)."""
        base = self._config.poll_interval_sec
        if (self._in_auction(now or self._now()) or self._pending
                or self._submit_failures):
            # 연속 발주 실패도 백오프(아키텍트 Minor — 브로커 장애 중 1초
            # 간격 재시도로 레이트리밋 소모 방지)
            return base * 5
        return base

    async def poll_once(self, positions: list[TradePosition],
                        now: datetime | None = None) -> list[ExitAction]:
        """감시 1사이클. positions는 호출자(Task 7)가 DB에서 읽은 ENTERED
        포지션 목록(trailing_active/peak는 DB 영속값 그대로 — 계약 상단 참조).
        반환: 이번 사이클에 집행/전이된 청산 액션 목록."""
        now = now or self._now()
        actions: list[ExitAction] = []
        if self._pending:
            actions.extend(await self._check_pending(now))
        active = [p for p in positions
                  if p.state is PositionState.ENTERED
                  and p.symbol not in self._pending]
        if not active:
            return actions

        quotes = await self._fetch_quotes([p.symbol for p in active])
        if quotes is None:
            return actions  # 전체 조회 실패 — 판정 없음(가격 불변 아님)

        due: list[tuple[TradePosition, MarketData, ExitReason]] = []
        for pos in active:
            md = quotes.get(pos.symbol)
            if md is None:
                await self._note_symbol_missing(pos.symbol)
                continue
            self._symbol_failures.pop(pos.symbol, None)
            self._warnings.pop(f"quote:{pos.symbol}", None)
            evaluated = self._evaluate(pos, md, now)
            if evaluated is not None:
                due.append(evaluated)

        # 집행 우선순위: 기간초과→손절→트레일링→익절(§6-2 선언 순).
        # **병렬 집행**(트레이더 P5-T6b C3): 직렬이면 첫 종목의 체결 대기
        # (동시호가 구간 최대 ~10분)가 후순위 종목의 **주문 제출 자체**를
        # 막아 15:30 매칭을 놓친다 — 태스크 생성은 우선순위 순(제출 순서
        # 보존), 체결 폴은 동시 진행. 개별 실패는 다른 종목을 죽이지 않는다.
        due.sort(key=lambda t: _REASON_PRIORITY[t[2]])
        if due:
            results = await asyncio.gather(
                *[self._execute_exit(pos, md, reason, now)
                  for pos, md, reason in due],
                return_exceptions=True)
            for (pos, _md, reason), result in zip(due, results):
                if isinstance(result, BaseException):
                    logger.error("exit execution crashed for %s: %s",
                                 pos.symbol, result)
                    actions.append(ExitAction(
                        pos.symbol, reason, PositionState.ENTERED,
                        pos.quantity,
                        detail=f"exit execution error "
                               f"({type(result).__name__}) — retry next cycle"))
                else:
                    actions.append(result)
        return actions

    # ── 판정 ────────────────────────────────────────────────────────────

    def _evaluate(self, pos: TradePosition, md: MarketData,
                  now: datetime) -> tuple[TradePosition, MarketData, ExitReason] | None:
        """순수 판정 호출 + 관측 결과(peak/래치) 즉시 영속. 청산 사유가 있으면
        (갱신된 포지션, 시세, 사유)를 반환."""
        if pos.entered_at is None:
            # ENTERED인데 진입 시각 없음 — 추적 오염. 보유기간 판정 불가라
            # 건너뛰되 침묵하지 않는다(§6-1).
            self._warnings[f"corrupt:{pos.symbol}"] = (
                f"{pos.symbol}: ENTERED without entered_at — "
                "holding-period rule cannot apply, reconcile required")
            logger.error("position %s has no entered_at — skipping evaluation",
                         pos.symbol)
            return None
        self._warnings.pop(f"corrupt:{pos.symbol}", None)  # 복구 시 해제
        entry_date = pos.entered_at.astimezone(self._calendar.KST).date()
        held = self._calendar.held_business_days(entry_date, now)
        evaluation = evaluate_exit(
            entry_price=pos.entry_price, current_price=md.quote.price,
            peak_price=pos.peak_price, trailing_active=pos.trailing_active,
            held_business_days=held, config=self._config)
        updated = replace(pos, peak_price=evaluation.new_peak,
                          trailing_active=evaluation.new_trailing_active)
        if (updated.peak_price != pos.peak_price
                or updated.trailing_active != pos.trailing_active):
            # 주문과 무관한 관측 영속 — 실패해도 이번 사이클 판정은 유효하고
            # 다음 관측이 같은 값을 재계산하므로 격리(경고 노출+로그). 단 청산
            # 발주 직전 persist(EXITING)는 fail-closed로 별도 수행된다.
            try:
                self._persist(updated)
                self._warnings.pop(f"persist:{pos.symbol}", None)
            except Exception as exc:  # noqa: BLE001
                self._warnings[f"persist:{pos.symbol}"] = (
                    f"{pos.symbol}: peak/trailing persist failing "
                    f"({type(exc).__name__}) — latch may reset on restart")
                logger.error("persist peak/trailing failed for %s: %s",
                             pos.symbol, exc)
        if evaluation.reason is None:
            return None
        return updated, md, evaluation.reason

    # ── 집행 (§6-2-b) ───────────────────────────────────────────────────

    def track_existing_exit(self, pos: TradePosition, order_no: str,
                            est_price: int | None = None) -> None:
        """재기동 reconcile ⑤(청산 **시장가** 주문 생존 — 취소 금지 계약)의
        _pending 시드(6c → Task 7). 인메모리 _pending은 재시작 시 소실되므로
        (보안 P5-T6b #4 고아 갭) reconcile이 식별한 생존 청산 주문을 여기로
        복원해야 다음 poll_once가 체결을 추적한다. est_price 미상(None)이면
        확정 시 pnl/exit_price를 기록하지 않는다(트레이더 I4) — pending 확정은
        항상 requires_reconcile=True라 잔고 대사가 실측으로 채운다."""
        reason = pos.exit_reason or ExitReason.STOP_LOSS  # 미상 시 보수 라벨
        # est_price 기본 None — 신뢰할 추정이 없으면 pnl을 기록하지 않는다
        # (트레이더 I4). seen_alive=False — 부재=체결에 연속 2회 확인(보안 #2).
        self._pending[pos.symbol] = _PendingExit(pos, order_no, reason,
                                                 est_price, seen_alive=False)
        self._warnings[f"pending:{pos.symbol}"] = (
            f"{pos.symbol}: exit order {order_no} restored from reconcile — "
            "tracking")

    async def liquidate(self, pos: TradePosition,
                        now: datetime | None = None) -> ExitAction:
        """킬스위치 LIQUIDATE_ALL(§8-1-b) — Task 7이 호출. 즉시 시장가 청산.
        시세 조회 실패는 청산을 막지 않는다(속도 우선 — 추정가만 보수적으로
        peak 사용: caps 검증에 상방 추정이 안전 방향).

        중복 매도 가드(보안 P5-T6b #1): 이미 청산 주문이 _pending으로 추적
        중이거나 ENTERED가 아닌 포지션에는 두 번째 매도를 내지 않는다 —
        poll_once의 active 필터와 동일한 방어를 킬스위치 경로에도 대칭 적용."""
        now = now or self._now()
        if pos.symbol in self._pending:
            pending = self._pending[pos.symbol]
            return ExitAction(pos.symbol, ExitReason.KILL_SWITCH,
                              PositionState.EXITING, pos.quantity,
                              detail=f"exit order {pending.order_no} already "
                                     "pending — no duplicate sell")
        if pos.state is not PositionState.ENTERED:
            return ExitAction(pos.symbol, ExitReason.KILL_SWITCH, pos.state,
                              pos.quantity,
                              detail=f"not ENTERED ({pos.state.value}) — "
                                     "liquidate skipped")
        quotes = await self._fetch_quotes([pos.symbol])
        md = (quotes or {}).get(pos.symbol)
        est_price = (md.bid if md and md.bid > 0
                     else md.quote.price if md else pos.peak_price)
        return await self._exit_market(pos, est_price,
                                       ExitReason.KILL_SWITCH, now)

    async def _execute_exit(self, pos: TradePosition, md: MarketData,
                            reason: ExitReason, now: datetime) -> ExitAction:
        if reason is ExitReason.TAKE_PROFIT:
            return await self._exit_take_profit(pos, md, now)
        # 매도 추정 = 최우선 매수호가(1호가). ⚠️ 급락 구간(손절 발동 시점)은
        # 호가창이 얇아 1호가 잔량 초과분이 더 낮게 체결될 수 있다 — 낙관적
        # 추정(트레이더 I4). realized_pnl은 잔고 대사 전까지 추정치다.
        est_price = md.bid if md.bid > 0 else md.quote.price
        return await self._exit_market(pos, est_price, reason, now)

    async def _exit_market(self, pos: TradePosition, est_price: int,
                           reason: ExitReason, now: datetime) -> ExitAction:
        """손절/트레일링/기간초과/킬스위치 — 시장가 즉시, 폴백 없음(§6-2-b:
        미청산 리스크 > 슬리피지)."""
        # exit_phase 명시 clear — 시장가 청산은 서브상태가 없다. 스테일 phase
        # (직전 TP 시도 잔재)가 영속되면 재기동 reconcile이 "익절 지정가
        # 생존"으로 오판해 살아있는 시장가 매도를 취소한다(아키텍트 P5-T6c #2).
        exiting = replace(pos, state=PositionState.EXITING, exit_reason=reason,
                          exit_phase=None)
        req = OrderRequest(symbol=pos.symbol, side=OrderSide.SELL,
                           style=OrderStyle.MARKET, quantity=pos.quantity)
        ack = await self._submit_exit(exiting, req, est_price)
        if ack is None:
            return self._submit_failed_action(pos, reason, now)
        unfilled = await self._poll(ack.order_no, now)
        if unfilled == 0:
            return self._close(exiting, est_price, reason, now)
        # 미체결/관측 전무 — 취소하지 않고 추적(동시호가·VI — 모듈 docstring)
        return self._track_pending(exiting, ack.order_no, reason, est_price,
                                   observed_nothing=unfilled < 0)

    async def _exit_take_profit(self, pos: TradePosition, md: MarketData,
                                now: datetime) -> ExitAction:
        """고정 익절 — 지정가(현재가) → exit_limit_timeout_sec(5s) → 취소 →
        시장가 폴백(§6-2-b 표). 유일하게 취소가 허용된 청산 경로."""
        reason = ExitReason.TAKE_PROFIT
        # 매도 지정가 — 아래 방향 스냅(체결 우선; 관측 현재가는 보통 이미
        # 그리드 위라 no-op, 방어적 스냅만)
        limit_price = round_to_tick(md.quote.price, pos.market, "down")
        exiting = replace(pos, state=PositionState.EXITING, exit_reason=reason,
                          exit_phase=ExitPhase.LIMIT_SUBMITTED)
        req = OrderRequest(symbol=pos.symbol, side=OrderSide.SELL,
                           style=OrderStyle.LIMIT, quantity=pos.quantity,
                           limit_price=limit_price)
        ack = await self._submit_exit(exiting, req, limit_price)
        if ack is None:
            return self._submit_failed_action(pos, reason, now)
        unfilled = await self._poll(ack.order_no, now)
        if unfilled == 0:
            return self._close(exiting, limit_price, reason, now)
        if unfilled < 0:
            # 관측 전무 — 체결 여부 불명. 취소 강행하면 "이미 체결됐는데 취소
            # 실패"와 구분 불가(6a 이중매매 가드와 동일 원리) — 추적으로 위임.
            return self._track_pending(exiting, ack.order_no, reason,
                                       limit_price, observed_nothing=True)

        # 타임아웃 미체결 — 취소 후 시장가 폴백. 취소 직전 중간 상태를
        # fail-closed 영속(아키텍트 P5-T6b #3 — EntryPhase.CANCEL_REQUESTED와
        # 대칭: "취소됐는데 재발주 전 사망"을 reconcile이 식별하는 근거)
        cancel_pos = replace(exiting, exit_phase=ExitPhase.CANCEL_REQUESTED)
        self._persist(cancel_pos)
        try:
            cancel_ack = await self._orders.cancel_order(ack.order_no,
                                                         pos.symbol)
        except Exception as exc:  # noqa: BLE001 — 이중 매도 가드
            # 취소 실패 = 주문 상태 불명(직전 체결 가능). 시장가 재발주 강행
            # 시 이중 매도(초과 매도 거부/타 포지션 물량 오염) 위험 — 추적 위임.
            logger.error("TP limit cancel FAILED %s (order_no=%s): %s — "
                         "market fallback skipped, tracking", pos.symbol,
                         ack.order_no, exc)
            return self._track_pending(cancel_pos, ack.order_no, reason,
                                       limit_price, observed_nothing=True)
        audit_order(self._on_order, cancel_ack, req, "cancelled")

        filled = pos.quantity - unfilled
        remaining = unfilled
        # ⚠️ 스냅샷 quantity는 원 수량 유지 — 부분체결 시 실보유(remaining)와
        # 다를 수 있는 창(취소~폴백 크래시). reconcile(6c)은 quantity 필드가
        # 아니라 잔고(kt00018) ground truth로 수량을 재확정한다(아키텍트 #2).
        market_pos = replace(exiting, exit_phase=ExitPhase.MARKET_SUBMITTED)
        market_req = OrderRequest(symbol=pos.symbol, side=OrderSide.SELL,
                                  style=OrderStyle.MARKET, quantity=remaining)
        # 폴백 발주 실패 시 복원 스냅샷: 체결분이 이미 팔렸으므로 원 수량이
        # 아니라 **잔량**으로 ENTERED 복원(트레이더 C1 — 원 수량 복원은 다음
        # 사이클에서 이미 판 수량의 초과 매도를 유발). caps 추정가는
        # limit_price 재사용(매도 슬리피지는 대개 노출 감소 방향 — 보수적).
        survivor = replace(exiting, state=PositionState.ENTERED,
                           exit_reason=None, exit_phase=None,
                           quantity=remaining)
        market_ack = await self._submit_exit(
            market_pos, market_req, limit_price, quantity=remaining,
            revert=survivor if filled > 0 else None)
        if market_ack is None:
            if filled > 0:
                return ExitAction(
                    pos.symbol, reason, PositionState.ENTERED, remaining,
                    exit_price=limit_price,
                    detail=f"TP partial fill {filled}/{pos.quantity} sold; "
                           f"market fallback submit failed — monitoring "
                           f"remaining {remaining}",
                    requires_reconcile=True)
            return self._submit_failed_action(pos, reason, now)
        unfilled2 = await self._poll(market_ack.order_no, now)
        if unfilled2 == 0:
            # 혼합 체결(지정가 filled + 시장가 remaining) — 시장가분은 사이클
            # 초 관측 bid로 블렌디드 추정(개발자 #2: 시장가 체결을 스테일
            # 지정가로 치던 왜곡 제거). 부분체결 혼합은 잔고 대사 필수 표식.
            bid = md.bid if md.bid > 0 else md.quote.price
            blended = (filled * limit_price + remaining * bid) // pos.quantity
            return self._close(market_pos, blended, reason, now,
                               requires_reconcile=filled > 0)
        return self._track_pending(market_pos, market_ack.order_no, reason,
                                   limit_price, observed_nothing=unfilled2 < 0)

    async def _submit_exit(self, exiting: TradePosition, req: OrderRequest,
                           est_price: int, quantity: int | None = None,
                           revert: TradePosition | None = None) -> OrderAck | None:
        """청산 발주 공통: caps(발주 직전, §8-1 — side=SELL 전달: 구현체는
        매도를 차단하지 않는다, 트레이더 C2/아키텍트 #4 Task 7 계약) →
        persist(fail-closed) → 발주 → 감사. 실패 시 None 반환(호출자가
        재시도/고정 판단) — persist(EXITING) 후 발주가 실패하면 revert
        스냅샷(기본: ENTERED·원 수량, 부분체결 후 폴백은 잔량 수량 —
        트레이더 C1)으로 되돌려 다음 사이클 재시도가 필터에 걸리게 한다."""
        qty = quantity if quantity is not None else exiting.quantity
        symbol = exiting.symbol
        try:
            self._check_caps(est_price * qty, OrderSide.SELL)
            ack = await submit_order(self._orders, req,
                                     persist=lambda: self._persist(exiting),
                                     on_order=self._on_order)
        except Exception as exc:  # noqa: BLE001 — 청산 실패는 침묵 금지
            count = self._submit_failures.get(symbol, 0) + 1
            self._submit_failures[symbol] = count
            # 상태 API 노출 문자열엔 예외 **타입명만**(보안 P5-T6b #3) — DB
            # 드라이버 예외 원문은 DSN(자격증명) 포함 가능. 원문은 로그로만.
            self._warnings[f"exit:{symbol}"] = (
                f"{symbol}: exit order submit failed ({count}/"
                f"{_EXIT_SUBMIT_RETRY_LIMIT}): {type(exc).__name__}")
            logger.error("exit submit failed for %s (attempt %d): %s",
                         symbol, count, exc)
            fallback = revert if revert is not None else replace(
                exiting, state=PositionState.ENTERED,
                exit_reason=None, exit_phase=None)
            try:
                # EXITING이 영속된 뒤 발주가 실패했을 수 있음 — 복원
                self._persist(fallback)
            except Exception as revert_exc:  # noqa: BLE001
                logger.error("persist revert failed for %s: %s — reconcile "
                             "will recover", symbol, revert_exc)
            return None
        self._submit_failures.pop(symbol, None)
        self._warnings.pop(f"exit:{symbol}", None)
        return ack

    def _submit_failed_action(self, pos: TradePosition, reason: ExitReason,
                              now: datetime) -> ExitAction:
        """발주 실패 처리: 상한 미만이면 ENTERED 유지(다음 사이클 재시도),
        연속 상한 도달 시 EXIT_FAILED로 고정(§6-1 침묵 금지 — 상태 API 노출,
        수동 개입/reconcile ⑦ 대상)."""
        count = self._submit_failures.get(pos.symbol, 0)
        if count < _EXIT_SUBMIT_RETRY_LIMIT:
            return ExitAction(pos.symbol, reason, PositionState.ENTERED,
                              pos.quantity,
                              detail=f"exit submit failed (attempt {count}) — "
                                     "retrying next cycle")
        failed = replace(pos, state=PositionState.EXIT_FAILED,
                         exit_reason=reason, exit_phase=None)
        try:
            self._persist(failed)
        except Exception as exc:  # noqa: BLE001
            logger.error("persist EXIT_FAILED failed for %s: %s", pos.symbol,
                         exc)
        self._clear_symbol_state(pos.symbol)  # 카운터 정리 후 최종 경고만 유지
        self._warnings[f"exit:{pos.symbol}"] = (
            f"{pos.symbol}: exit submit failed {count} times — EXIT_FAILED, "
            "manual intervention required")
        return ExitAction(pos.symbol, reason, PositionState.EXIT_FAILED,
                          pos.quantity, requires_reconcile=True,
                          detail="exit submit retry limit exhausted")

    # ── 체결 확정/추적 ──────────────────────────────────────────────────

    def _close(self, pos: TradePosition, sell_price: int | None,
               reason: ExitReason, now: datetime,
               requires_reconcile: bool = False) -> ExitAction:
        """청산 확정. requires_reconcile: 추정치 신뢰도가 낮은 경로(부분체결
        혼합·pending 지연 확정)의 즉시 잔고 대사 표식(트레이더 I5/I6).
        sell_price=None(reconcile 시드 — 신뢰할 추정 없음)이면 pnl/exit_price를
        기록하지 않고(None) 잔고 대사를 강제 표식한다(트레이더 I4 — 과소평가
        손실이 확정 숫자처럼 남는 것 방지)."""
        pnl = (realized_pnl(pos.market, pos.entry_price * pos.quantity,
                            sell_price * pos.quantity, self._config)
               if sell_price is not None else None)
        closed = replace(pos, state=PositionState.CLOSED, exit_price=sell_price,
                         exit_reason=reason, realized_pnl=pnl, closed_at=now,
                         exit_phase=None)
        needs = requires_reconcile or sell_price is None
        try:
            self._persist(closed)  # 체결 후 — 격리(모듈 상단 PersistPosition 계약)
        except Exception as exc:  # noqa: BLE001
            needs = True
            logger.error("persist CLOSED failed for %s: %s — reconcile will "
                         "recover from balance", pos.symbol, exc)
        self._clear_symbol_state(pos.symbol)
        return ExitAction(pos.symbol, reason, PositionState.CLOSED,
                          pos.quantity, exit_price=sell_price,
                          realized_pnl=pnl, requires_reconcile=needs)

    def _clear_symbol_state(self, symbol: str) -> None:
        """포지션 종결(CLOSED/EXIT_FAILED 확정) 시 심볼 귀속 카운터·경고 정리
        (개발자 P5-T6b #3 — 장수명 인스턴스에서 동일 심볼 재진입 포지션이
        과거 실패 카운트를 물려받아 첫 실패에 EXIT_FAILED로 고정되는 누수
        방지)."""
        self._submit_failures.pop(symbol, None)
        self._symbol_failures.pop(symbol, None)
        for prefix in ("exit", "quote", "pending", "persist", "corrupt"):
            self._warnings.pop(f"{prefix}:{symbol}", None)

    def _track_pending(self, pos: TradePosition, order_no: str,
                       reason: ExitReason, est_price: int,
                       observed_nothing: bool) -> ExitAction:
        # 관측 전무(observed_nothing) pending은 등록 자체가 미확인 — 부재=체결
        # 판정에 연속 2회 확인 요구(보안 P5-T6c #2, C1 방어 대칭)
        self._pending[pos.symbol] = _PendingExit(
            pos, order_no, reason, est_price,
            seen_alive=not observed_nothing)
        self._warnings[f"pending:{pos.symbol}"] = (
            f"{pos.symbol}: exit order {order_no} unfilled — tracking "
            "(auction/VI window not cancelled)")
        return ExitAction(pos.symbol, reason, PositionState.EXITING,
                          pos.quantity, exit_price=est_price,
                          detail=f"unfilled — tracking order {order_no}",
                          requires_reconcile=observed_nothing)

    async def _check_pending(self, now: datetime) -> list[ExitAction]:
        """미확정 청산 주문 재확인. get_open_orders 실패(BrokerError)는 경고+
        유지(전면 중단 금지 — P5-T4 아키텍트 이월). 부재=체결(제출 사이클에서
        최소 1회 폴링·감사됐고 우리가 취소하지 않은 주문 — 잔여 전파 레이스는
        Task 7 잔고 대사가 흡수). 장 마감 후에도 생존한 주문은 EXIT_FAILED."""
        try:
            open_orders = await self._orders.get_open_orders()
        except Exception as exc:  # noqa: BLE001
            # 매도 주문이 이미 나간 상태의 무소식 — 임계 초과 시 상태 API로
            # 승격(보안 P5-T6b #2: 로그만으로는 '정상'으로 보이는 침묵 실패)
            self._pending_check_failures += 1
            logger.warning("pending-exit check failed (%s) — keeping %d "
                           "pending", exc, len(self._pending))
            if self._pending_check_failures > self._config.quote_failure_threshold:
                self._warnings["pending:check"] = (
                    f"pending-exit check failing "
                    f"({self._pending_check_failures} consecutive) — "
                    f"{len(self._pending)} exit order(s) unverifiable")
            return []
        self._pending_check_failures = 0
        self._warnings.pop("pending:check", None)
        alive = {o.order_no for o in open_orders}
        actions: list[ExitAction] = []
        for symbol, pending in list(self._pending.items()):
            if pending.order_no in alive and not pending.seen_alive:
                # 등록 관측 확정 — 이후 부재는 즉시 체결 판정 가능
                self._pending[symbol] = replace(pending, seen_alive=True,
                                                absent_streak=0)
                continue
            if pending.order_no not in alive:
                if not pending.seen_alive and pending.absent_streak + 1 < 2:
                    # 미관측 pending의 첫 부재 — 체결/미전파/오시드 구분 불가
                    # (보안 P5-T6c #2): 연속 2회 확인 전에는 CLOSED 금지
                    self._pending[symbol] = replace(
                        pending, absent_streak=pending.absent_streak + 1)
                    continue
                del self._pending[symbol]
                # est_price가 제출 시점 관측치라 지연 확정일수록 괴리가 큼
                # (트레이더 I6) + 미추적 창 체결 — 즉시 잔고 대사 표식.
                actions.append(self._close(pending.position, pending.est_price,
                                           pending.reason, now,
                                           requires_reconcile=True))
            elif not self._calendar.is_market_hours(now):
                # 장 마감 — 미체결 주문은 당일로 소멸. 청산 실패 고정.
                del self._pending[symbol]
                failed = replace(pending.position,
                                 state=PositionState.EXIT_FAILED,
                                 exit_phase=None)
                try:
                    self._persist(failed)
                except Exception as exc:  # noqa: BLE001
                    logger.error("persist EXIT_FAILED failed for %s: %s",
                                 symbol, exc)
                self._clear_symbol_state(symbol)
                self._warnings[f"exit:{symbol}"] = (
                    f"{symbol}: exit order survived to market close — "
                    "EXIT_FAILED, position still held")
                actions.append(ExitAction(
                    symbol, pending.reason, PositionState.EXIT_FAILED,
                    pending.position.quantity, requires_reconcile=True,
                    detail="exit order unfilled at market close"))
        return actions

    # ── 시세 조회/실패 구분 (§6-4) ──────────────────────────────────────

    async def _fetch_quotes(self,
                            symbols: list[str]) -> dict[str, MarketData] | None:
        try:
            quotes = await self._orders.get_quotes(symbols)
        except Exception as exc:  # noqa: BLE001
            self._quote_failures += 1
            logger.warning("quote poll failed (%d consecutive): %s",
                           self._quote_failures, exc)
            if self._quote_failures > self._config.quote_failure_threshold:
                # 키는 "그룹:항목" 규약 통일(개발자 Minor — 소비자 그룹핑)
                self._warnings["quote:all"] = (
                    f"quote polling failing ({self._quote_failures} "
                    "consecutive) — positions unmonitored, polling continues")
            return None
        self._quote_failures = 0
        self._warnings.pop("quote:all", None)
        return {md.quote.symbol: md for md in quotes}

    async def _note_symbol_missing(self, symbol: str) -> None:
        """특정 종목만 응답 결측 — 임계 초과 시 거래정지 vs 네트워크 구분."""
        count = self._symbol_failures.get(symbol, 0) + 1
        self._symbol_failures[symbol] = count
        if count <= self._config.quote_failure_threshold:
            return
        state = None
        if self._lookup_state is not None:
            try:
                state = await self._lookup_state(symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning("instrument state lookup failed for %s: %s",
                               symbol, exc)
        if state and "정지" in state:
            self._warnings[f"quote:{symbol}"] = (
                f"{symbol}: trading halted ({state}) — auto-exit impossible, "
                "manual attention required")
        else:
            self._warnings[f"quote:{symbol}"] = (
                f"{symbol}: quote missing {count} consecutive polls "
                "(not halted — network/feed issue suspected)")

    # ── 내부 유틸 ───────────────────────────────────────────────────────

    def _in_auction(self, now: datetime) -> bool:
        t = now.astimezone(self._calendar.KST).time()
        return _AUCTION_START <= t < time(15, 30)

    def _fill_timeout(self, now: datetime) -> float:
        """체결 확인 데드라인. 평시엔 exit_limit_timeout_sec — 동시호가 구간
        (15:20~)에는 15:30 매칭+여유까지 연장(§6-4: 즉시 체결 가정 비적용)."""
        base = self._config.exit_limit_timeout_sec
        kst = now.astimezone(self._calendar.KST)
        if not self._in_auction(now):
            return base
        match_at = kst.replace(hour=15, minute=30, second=0, microsecond=0)
        until_match = (match_at - kst).total_seconds()
        return max(base, until_match + _AUCTION_MATCH_GRACE_SEC)

    async def _poll(self, order_no: str, now: datetime) -> int:
        return await poll_unfilled(
            self._orders, order_no, timeout_sec=self._fill_timeout(now),
            interval_sec=self._config.poll_interval_sec, sleep=self._sleep)
