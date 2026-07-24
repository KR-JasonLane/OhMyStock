"""재기동 대조(reconcile) — DB 미종결 포지션 ↔ 브로커 실제 상태(ground truth)
정합(스펙 §6-6). TradingService의 실행 상태(`BackgroundRunService._running`)와
monitor의 `_pending` 추적은 인메모리라 재시작 시 무조건 소실 — 이 절차가
없으면 실보유가 손절 감시 밖에 방치된다(클라이언트측 TP/SL 전제 붕괴).

구조(계획서 Task 6c): `reconcile_decide`(순수 — 입력: DB 상태+브로커 상태 →
출력: 조정 액션 목록, 전수 테스트 가능)와 `apply_reconcile`(부수효과 —
취소·영속, 콜백 주입: store 통짜 금지, 6a/6b 패턴)를 이름으로 분리.

판정 원칙:
- **브로커 관측과 전략 소유권을 분리** — 주문 생존은 ka10075(주문번호 명시
  연결 — DbPosition.order_nos, symbol 매칭 금지), 실제 무보유는 kt00018
  잔고로 확인한다. 다만 kt00018 종목 합계의 양수 수량은 수동 거래와 전략
  수량을 구분하지 못하므로 DB 전략 수량으로 자동 정렬하지 않는다.
- **진입 창 경계(§6-6.3):** reconcile은 감시·청산만 재개한다. 진입성 주문은
  창 밖이면 취소만(시장가 재발주 = 실질 신규 진입 — 금지).
- **청산 주문은 성급히 취소하지 않는다**(6b 계약): 시장가 청산 생존은
  RESUME_EXIT_WATCH(monitor.track_existing_exit 시드). 익절 **지정가** 생존만
  취소 대상(⑤ — 취소 후 ENTERED 복귀, 다음 poll_once가 즉시 재평가해 사유
  유효 시 §6-2-b로 마무리: "남은 타임아웃 무시" 요구와 동치).
- 취소 실패 = 주문 상태 불명 → 포지션 상태를 바꾸지 않고 경고(재실행/수동
  위임 — 6a/6b 이중매매 가드와 동일 원리)."""

import enum
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace

from app.domain.broker import (Balance, OpenOrder, OrderAck, OrderPort,
                               OrderSide)
from app.domain.trading.execution import PersistPosition
from app.domain.trading.models import (EntryPhase, ExitPhase, PositionState,
                                       TradePosition)

logger = logging.getLogger(__name__)
# 취소 감사 콜백 — (ack, action). action이 심볼·kind(진입/청산 방향 유추)·
# cancel_order_no를 담아 감사 행의 정확성을 보장한다(보안 P5-T7 Minor —
# 하드코딩 심볼/방향 금지). execution.OnOrder는 OrderRequest 필수라 재사용
# 불가(취소엔 원 요청이 없음). 격리 정책은 동일: 취소는 이미 나갔으므로
# 기록 실패가 나머지 정합을 죽이면 안 된다.
RecordCancel = Callable[[OrderAck, "ReconcileAction"], None]


class ReconcileKind(enum.Enum):
    """조정 액션 유형 — 스펙 §6-6 분기 ①~⑦(+⑤-b/⑥-b)의 실행 형태."""
    RESUME_ENTRY_WATCH = "resume_entry_watch"  # ②(창 안): 진입 주문 감시 재개
    # ②(창 밖)/취소 미완 중 잔고 0: 취소 → ENTRY_FAILED 확정.
    # 양수 잔고는 OWNERSHIP_AMBIGUOUS로 별도 격리한다.
    CANCEL_AND_SETTLE_ENTRY = "cancel_and_settle_entry"
    FAIL_ENTRY = "fail_entry"                # ③: 고아 취소 → ENTRY_FAILED + 알람
    CLOSE = "close"                          # ④/⑦무보유/⑥-b: CLOSED 확정
    CANCEL_AND_REWATCH = "cancel_and_rewatch"  # ⑤(익절 지정가 생존): 취소→감시 복귀
    RESUME_EXIT_WATCH = "resume_exit_watch"  # ⑤(시장가 생존): 취소 금지, pending 시드
    REWATCH = "rewatch"                      # ⑤-b/잔존 보유: ENTERED 복귀(수량 정합)
    OWNERSHIP_AMBIGUOUS = "ownership_ambiguous"  # 계좌합계의 전략 귀속 불가
    CANCEL_FAILED = "cancel_failed"              # live 주문 취소 상태 불명
    WARN = "warn"                            # ⑥/⑦보유: 수동 개입 경고만


@dataclass(frozen=True)
class DbPosition:
    """decide 입력 1건 — 포지션 + DB에 기록된 미종결 주문번호(명시 연결 §6-6.②:
    Task 7이 trade_orders.trade_position_id로 조회해 구성)."""
    position: TradePosition
    order_nos: tuple[str, ...] = ()
    unmanaged_qty: int = 0


@dataclass(frozen=True)
class ReconcileAction:
    kind: ReconcileKind
    symbol: str
    # 조정 후 영속할 스냅샷(WARN/RESUME_ENTRY_WATCH 등 무영속 액션은 None)
    position: TradePosition | None = None
    cancel_order_no: str | None = None   # 취소 대상(CANCEL_* 계열)
    cancel_side: OrderSide | None = None  # 취소 감사의 원주문 방향
    watch_order_no: str | None = None    # 감시 재개 대상(RESUME_* 계열)
    note: str = ""
    # 수동 확인이 필요한 분기(③ 고아취소/⑥ 계열/수량 불일치) — 구조화 필드
    # (문자열 매칭 분기 금지 계약과 동일 원칙). apply가 warnings로 승격.
    alarm: bool = False


def reconcile_decide(db_positions: list[DbPosition],
                     broker_open_orders: list[OpenOrder],
                     broker_balance: Balance,
                     in_entry_window: bool) -> list[ReconcileAction]:
    """순수 판정 — §6-6 분기 전수. 브로커 상태(주문 생존/잔고 수량)가 ground
    truth. 시장가 미확정 케이스는 별도 분기 없이 ①(체결 완료)/②·⑤(주문
    생존)로 자연 흡수된다(§6-6 명시)."""
    live_by_no = {order.order_no: order for order in broker_open_orders}
    held = {p.symbol: p.quantity for p in broker_balance.positions
            if p.quantity > 0}
    actions: list[ReconcileAction] = []
    for db in db_positions:
        expected_side = (
            OrderSide.BUY
            if db.position.state is PositionState.PENDING_ENTRY
            else OrderSide.SELL
            if db.position.state is PositionState.EXITING
            else None)
        live_order = next((
            order.order_no for order_no in db.order_nos
            if (order := live_by_no.get(order_no)) is not None
            and order.side is expected_side), None)
        blocking_live_buy = any(
            order.symbol == db.position.symbol
            and order.side is OrderSide.BUY
            and not (
                db.position.state is PositionState.PENDING_ENTRY
                and order.order_no in db.order_nos)
            for order in broker_open_orders)
        action = _decide_one(
            db, live_order, held, in_entry_window,
            blocking_live_buy=blocking_live_buy)
        if action is not None:
            actions.append(action)
    # ⑥ DB엔 없는데 브로커 잔고에 있음 → 경고(수동 개입)
    db_symbols = {d.position.symbol for d in db_positions}
    for symbol, qty in sorted(held.items()):
        if symbol not in db_symbols:
            actions.append(ReconcileAction(
                ReconcileKind.WARN, symbol, alarm=True,
                note=f"⑥ {symbol}: broker holds {qty} but no DB position — "
                     "manual intervention required"))
    return actions


def _decide_one(db: DbPosition, live_order: str | None,
                held: dict[str, int],
                in_entry_window: bool, *,
                blocking_live_buy: bool = False) -> ReconcileAction | None:
    pos = db.position
    broker_qty = held.get(pos.symbol, 0)
    if blocking_live_buy:
        ambiguous = _ownership_ambiguous(
            pos, broker_qty, db.unmanaged_qty,
            note_prefix=f"{pos.state.value} has unattributed live BUY")
        if pos.state is PositionState.PENDING_ENTRY and live_order is not None:
            # 미귀속 BUY는 건드리지 않되 DB 주문번호로 귀속이 확실한 BUY는
            # 추가 노출 확대를 막기 위해 취소한다.
            return replace(
                ambiguous, cancel_order_no=live_order,
                cancel_side=OrderSide.BUY)
        if pos.state is PositionState.EXITING and live_order is not None:
            # 방향이 확인된 기존 SELL은 추적을 잃지 않는다.
            return replace(ambiguous, watch_order_no=live_order)
        return ambiguous
    if pos.state is PositionState.PENDING_ENTRY:
        return _decide_pending_entry(pos, live_order, broker_qty,
                                     in_entry_window, db.unmanaged_qty)
    if pos.state is PositionState.ENTERED:
        return _decide_entered(pos, broker_qty, db.unmanaged_qty)
    if pos.state is PositionState.EXITING:
        return _decide_exiting(
            pos, live_order, broker_qty, db.unmanaged_qty)
    if pos.state is PositionState.EXIT_FAILED:
        return _decide_exit_failed(pos, broker_qty, db.unmanaged_qty)
    return None  # CLOSED/ENTRY_FAILED는 미종결 조회에 없음(방어)


def _decide_pending_entry(pos: TradePosition, live_order: str | None,
                          broker_qty: int,
                          in_entry_window: bool,
                          unmanaged_qty: int = 0) -> ReconcileAction:
    symbol = pos.symbol
    if live_order is not None:
        if pos.entry_phase is not EntryPhase.CANCEL_REQUESTED and in_entry_window:
            # ② 미체결 진입 주문 생존 + 창 안 → 감시 재개
            return ReconcileAction(ReconcileKind.RESUME_ENTRY_WATCH, symbol,
                                   watch_order_no=live_order,
                                   note="② entry order alive — resume watch")
        # 창 밖(취소만 — 시장가 재발주 금지 §6-6.3) 또는 취소 미완(의도 유지).
        # 양수 계좌 합계는 검증 전 crash 창의 수동 거래와 전략 체결을 분리할
        # 수 없으므로 취소 뒤에도 전략 수량으로 승격하지 않는다.
        why = ("cancel was in-flight" if pos.entry_phase is
               EntryPhase.CANCEL_REQUESTED else "outside entry window")
        if broker_qty > 0:
            ambiguous = _ownership_ambiguous(
                pos, broker_qty, unmanaged_qty,
                note_prefix=f"② {why} pending-entry ownership")
            return replace(
                ambiguous, cancel_order_no=live_order,
                cancel_side=OrderSide.BUY)
        return ReconcileAction(ReconcileKind.CANCEL_AND_SETTLE_ENTRY, symbol,
                               position=None,
                               cancel_order_no=live_order,
                               cancel_side=OrderSide.BUY,
                               note=f"② {why} — cancel, then settle by fresh "
                                    "balance")
    if broker_qty > 0:
        # 주문별 fill 근거와 post-entry baseline을 기록하기 전에 죽은 창.
        # 종목 합계를 전략 체결량으로 승격하지 않는다.
        return _ownership_ambiguous(
            pos, broker_qty, unmanaged_qty,
            note_prefix="① pending entry order gone")
    # 주문도 보유도 없음: ③ 고아 취소(CANCEL_REQUESTED) 또는 소멸·미체결
    label = ("③ orphan cancel" if pos.entry_phase is
             EntryPhase.CANCEL_REQUESTED else "entry order gone, no holdings")
    return ReconcileAction(ReconcileKind.FAIL_ENTRY, symbol,
                           position=_entry_failed(pos), alarm=True,
                           note=f"{label} — ENTRY_FAILED (no market re-entry: "
                                "signal window passed)")


def _decide_entered(
        pos: TradePosition, broker_qty: int,
        unmanaged_qty: int = 0) -> ReconcileAction | None:
    if broker_qty == 0:
        # ⑥-b 외부 처분 추정 — ENTERED로 두면 없는 물량에 매도를 시도한다
        return ReconcileAction(
            ReconcileKind.CLOSE, pos.symbol,
            position=replace(pos, state=PositionState.CLOSED), alarm=True,
            note="⑥-b ENTERED but broker holds none — external disposal "
                 "assumed, CLOSED (pnl unresolved, manual audit)")
    if unmanaged_qty > 0 or broker_qty != pos.quantity:
        return _ownership_ambiguous(
            pos, broker_qty, unmanaged_qty,
            note_prefix="⑥-b ENTERED ownership")
    return None  # 정합 — 조정 불요


def _decide_exiting(pos: TradePosition, live_order: str | None,
                    broker_qty: int,
                    unmanaged_qty: int = 0) -> ReconcileAction:
    symbol = pos.symbol
    if pos.exit_reason is None:
        # ownership quarantine의 durable discriminator. 귀속 SELL은 취소·
        # 재평가하지 않고 기존 주문만 추적하며 수동 해제 전에는 유지한다.
        ambiguous = _ownership_ambiguous(
            pos, broker_qty, unmanaged_qty,
            note_prefix="ownership quarantine requires manual release")
        return (
            replace(ambiguous, watch_order_no=live_order)
            if live_order is not None else ambiguous)
    if live_order is not None:
        if unmanaged_qty > 0:
            # 혼합 소유 심볼에서는 계좌 합계만으로 취소 후 전략 잔량을
            # 재개방할 수 없다. 이미 살아 있는 주문만 계속 추적하고,
            # 소멸 뒤에는 아래 ownership ambiguity로 수동 대사한다.
            return ReconcileAction(
                ReconcileKind.RESUME_EXIT_WATCH, symbol,
                watch_order_no=live_order, alarm=True,
                note="⑤ mixed ownership with live exit order — track "
                     "existing order only; manual reconciliation required")
        stale_limit = pos.exit_phase in (ExitPhase.LIMIT_SUBMITTED,
                                         ExitPhase.CANCEL_REQUESTED)
        if stale_limit and broker_qty > 0:
            if broker_qty != pos.quantity:
                ambiguous = _ownership_ambiguous(
                    pos, broker_qty, unmanaged_qty,
                    note_prefix="⑤ stale limit quantity")
                return replace(
                    ambiguous, cancel_order_no=live_order,
                    cancel_side=OrderSide.SELL)
            # ⑤ 익절 지정가 생존(취소 미완 CANCEL_REQUESTED 포함 — 트레이더
            # P5-T6c C1: 취소 의도가 있던 스테일 지정가를 "추적"으로 살리면
            # 그 종목이 손절/트레일링 재평가에서 완전히 배제된다. 진입측
            # CANCEL_REQUESTED의 "취소 의도 유지"와 대칭) → 취소 후 감시
            # 복귀 — 다음 poll_once가 남은 타임아웃 무시하고 즉시 재평가
            # (사유 유효 시 §6-2-b 재집행). baseline0이고 DB 전략 수량과
            # 계좌 합계가 정확히 같은 경우에만 감시로 복귀한다.
            return ReconcileAction(
                ReconcileKind.CANCEL_AND_REWATCH, symbol,
                position=_rewatch(pos, broker_qty),
                cancel_order_no=live_order,
                cancel_side=OrderSide.SELL,
                note="⑤ stale TP limit alive — cancel, re-evaluate next cycle")
        # ⑤ **시장가** 청산 주문 생존(MARKET_SUBMITTED / exit_phase=None인
        # 손절·트레일링·기간초과) — 취소 금지(6b 계약: 동시호가·VI 대기 가능,
        # 매도는 완결돼야 한다) → pending 시드. 지정가 생존+잔고 0(전량 체결
        # 직후 전파 지연 등 모호 — 개발자 #5: 취소+수량 0 복귀는 불가능한
        # 상태)도 취소 대신 추적 위임 — pending의 소멸=체결 확인이 해소한다.
        # persist 없음(RESUME 계열 I/O 없음 계약 — position=None, 개발자 #1).
        return ReconcileAction(ReconcileKind.RESUME_EXIT_WATCH, symbol,
                               watch_order_no=live_order,
                               note="⑤ exit order alive — resume pending "
                                    "watch (no cancel)")
    if broker_qty == 0:
        # ④ 청산 완료(시장가 미확정 흡수 포함)
        return ReconcileAction(
            ReconcileKind.CLOSE, symbol,
            position=replace(pos, state=PositionState.CLOSED,
                             exit_phase=None),
            note="④ exit completed — CLOSED (pnl from balance audit)")
    # 주문별 체결 근거 없이 계좌 합계의 양수 잔고를 전략 잔량으로 재개방하면
    # 같은 종목의 수동 거래를 자동 매도할 수 있다.
    return _ownership_ambiguous(
        pos, broker_qty, unmanaged_qty,
        note_prefix="⑤-b no exit order")


def _decide_exit_failed(
        pos: TradePosition, broker_qty: int,
        unmanaged_qty: int = 0) -> ReconcileAction:
    # live_order를 참조하지 않는 이유: KRX 정규장 주문은 전부 당일 유효(Day)라
    # EXIT_FAILED로 고정된 전일 주문이 재기동 시점에 잔존할 수 없다(트레이더
    # Minor — GTC/시간외 주문을 지원하게 되면 이 전제를 재검토할 것).
    if pos.exit_reason is None:
        return _ownership_ambiguous(
            pos, broker_qty, unmanaged_qty,
            note_prefix="ownership quarantine survived market close")
    if broker_qty == 0:
        # ⑦ 직전 매도가 실제로는 나갔던 것 — 확인 실패였을 뿐
        return ReconcileAction(
            ReconcileKind.CLOSE, pos.symbol,
            position=replace(pos, state=PositionState.CLOSED,
                             exit_phase=None),
            note="⑦ EXIT_FAILED but broker holds none — CLOSED confirmed")
    # ⑦ 보유 잔존 — 자동 재청산 금지(재시도 소진 상태, 무한루프 방지)
    return ReconcileAction(
        ReconcileKind.WARN, pos.symbol, alarm=True,
        note=f"⑦ EXIT_FAILED still holding {broker_qty} — NO auto "
             "re-liquidation (retries exhausted), manual intervention")


def _entry_failed(pos: TradePosition) -> TradePosition:
    return replace(pos, state=PositionState.ENTRY_FAILED, entry_phase=None)


def _rewatch(pos: TradePosition, qty: int) -> TradePosition:
    return replace(pos, state=PositionState.ENTERED, exit_phase=None,
                   exit_reason=None, quantity=qty)


def _ownership_ambiguous(
        pos: TradePosition, broker_qty: int, unmanaged_qty: int, *,
        note_prefix: str) -> ReconcileAction:
    return ReconcileAction(
        ReconcileKind.OWNERSHIP_AMBIGUOUS, pos.symbol,
        position=replace(
            pos, state=PositionState.EXITING, exit_reason=None),
        alarm=True,
        note=f"{note_prefix}: db_managed={pos.quantity}, "
             f"unmanaged_baseline={unmanaged_qty}, broker_total={broker_qty} "
             "— EXITING retained; do not auto-align or sell, manual "
             "reconciliation required")


async def apply_reconcile(actions: list[ReconcileAction], orders: OrderPort,
                          persist_position: PersistPosition,
                          record_cancel: RecordCancel | None = None,
                          ) -> tuple[list[ReconcileAction], list[str]]:
    """조정 액션 적용(부수효과). 반환: (적용 완료 액션, 경고 목록 — §6-7
    warnings 노출용).

    실패 정책:
    - 취소 실패 = 주문 상태 불명 → **포지션 영속을 건너뛰고** 경고(상태를
      바꾸면 살아있는 주문과 어긋난다 — 재실행/수동 위임).
    - 영속 실패 → 경고 + 다음 액션 계속(한 건이 전체 정합을 죽이지 않는다).
      취소는 성공했는데 영속만 실패하면 position=None marker를 applied에
      남겨 소비자가 fresh balance alignment를 계속할 수 있게 한다.
    - RESUME_* / WARN은 여기서 I/O 없음 — Task 7이 감시 재개/노출을 담당."""
    applied: list[ReconcileAction] = []
    warnings: list[str] = []
    for action in actions:
        cancel_succeeded = False
        if action.alarm:
            warnings.append(action.note)  # 구조화 필드 기준(문자열 매칭 금지)
        if action.kind is ReconcileKind.WARN:
            applied.append(action)
            continue
        if action.cancel_order_no is not None:
            try:
                ack = await orders.cancel_order(action.cancel_order_no,
                                                action.symbol)
            except Exception as exc:  # noqa: BLE001 — 상태 불명 가드
                note = (
                    f"{action.symbol}: reconcile cancel failed for order "
                    f"{action.cancel_order_no} ({type(exc).__name__}) — "
                    "trading run must stop; rerun/manual required")
                warnings.append(note)
                logger.error("reconcile cancel failed %s (order_no=%s): %s",
                             action.symbol, action.cancel_order_no, exc)
                applied.append(replace(
                    action, kind=ReconcileKind.CANCEL_FAILED,
                    position=None, cancel_order_no=None, cancel_side=None,
                    note=note, alarm=True))
                continue
            cancel_succeeded = True
            if record_cancel is not None:
                try:  # 격리 — 취소는 이미 나갔다(6a/6b 감사 계약과 동일)
                    record_cancel(ack, action)
                except Exception as exc:  # noqa: BLE001
                    # 감사 실패도 상태 API로 노출(보안 P5-T6c #1 — 로그 유실
                    # 시 "취소가 있었다"는 사실 자체가 재구성 불가)
                    warnings.append(
                        f"{action.symbol}: reconcile cancel audit failed "
                        f"for order {action.cancel_order_no} "
                        f"({type(exc).__name__}) — manual audit "
                        "reconstruction needed")
                    logger.error("reconcile cancel audit failed %s: %s",
                                 action.symbol, exc)
        if action.position is not None:
            try:
                persist_position(action.position)
            except Exception as exc:  # noqa: BLE001
                warnings.append(
                    f"{action.symbol}: reconcile persist failed "
                    f"({type(exc).__name__}) — state may be stale, rerun "
                    "required")
                logger.error("reconcile persist failed %s: %s",
                             action.symbol, exc)
                if (cancel_succeeded
                        or action.kind is ReconcileKind.OWNERSHIP_AMBIGUOUS):
                    applied.append(replace(action, position=None))
                continue
        applied.append(action)
    return applied, warnings
