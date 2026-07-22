"""재기동 대조(reconcile) — DB 미종결 포지션 ↔ 브로커 실제 상태(ground truth)
정합(스펙 §6-6). TradingService의 실행 상태(`BackgroundRunService._running`)와
monitor의 `_pending` 추적은 인메모리라 재시작 시 무조건 소실 — 이 절차가
없으면 실보유가 손절 감시 밖에 방치된다(클라이언트측 TP/SL 전제 붕괴).

구조(계획서 Task 6c): `reconcile_decide`(순수 — 입력: DB 상태+브로커 상태 →
출력: 조정 액션 목록, 전수 테스트 가능)와 `apply_reconcile`(부수효과 —
취소·영속, 콜백 주입: store 통짜 금지, 6a/6b 패턴)를 이름으로 분리.

판정 원칙:
- **브로커가 ground truth** — 주문 생존은 ka10075(주문번호 명시 연결 —
  DbPosition.order_nos, symbol 매칭 금지), 보유·수량은 kt00018 잔고.
  DB quantity는 신뢰하지 않는다(§6-6 수량 ground truth — TP 부분체결 창).
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

from app.domain.broker import Balance, OpenOrder, OrderAck, OrderPort
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
    PROMOTE_ENTERED = "promote_entered"      # ①: 체결 완료 → ENTERED(잔고 수량)
    RESUME_ENTRY_WATCH = "resume_entry_watch"  # ②(창 안): 진입 주문 감시 재개
    # ②(창 밖)/취소 미완: 취소 → **잔고 기준 확정**(부분체결=ENTERED 잔고
    # 수량, 무체결=ENTRY_FAILED — 결과는 position.state가 말한다. 개발자
    # P5-T6c #2: "FAIL" 명명은 부분체결 ENTERED 결과와 모순이라 SETTLE로)
    CANCEL_AND_SETTLE_ENTRY = "cancel_and_settle_entry"
    FAIL_ENTRY = "fail_entry"                # ③: 고아 취소 → ENTRY_FAILED + 알람
    CLOSE = "close"                          # ④/⑦무보유/⑥-b: CLOSED 확정
    CANCEL_AND_REWATCH = "cancel_and_rewatch"  # ⑤(익절 지정가 생존): 취소→감시 복귀
    RESUME_EXIT_WATCH = "resume_exit_watch"  # ⑤(시장가 생존): 취소 금지, pending 시드
    REWATCH = "rewatch"                      # ⑤-b/잔존 보유: ENTERED 복귀(수량 정합)
    WARN = "warn"                            # ⑥/⑦보유: 수동 개입 경고만


@dataclass(frozen=True)
class DbPosition:
    """decide 입력 1건 — 포지션 + DB에 기록된 미종결 주문번호(명시 연결 §6-6.②:
    Task 7이 trade_orders.trade_position_id로 조회해 구성)."""
    position: TradePosition
    order_nos: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconcileAction:
    kind: ReconcileKind
    symbol: str
    # 조정 후 영속할 스냅샷(WARN/RESUME_ENTRY_WATCH 등 무영속 액션은 None)
    position: TradePosition | None = None
    cancel_order_no: str | None = None   # 취소 대상(CANCEL_* 계열)
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
    alive = {o.order_no for o in broker_open_orders}
    held = {p.symbol: p.quantity for p in broker_balance.positions
            if p.quantity > 0}
    actions: list[ReconcileAction] = []
    for db in db_positions:
        action = _decide_one(db, alive, held, in_entry_window)
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


def _decide_one(db: DbPosition, alive: set[str], held: dict[str, int],
                in_entry_window: bool) -> ReconcileAction | None:
    pos = db.position
    live_order = next((no for no in db.order_nos if no in alive), None)
    broker_qty = held.get(pos.symbol, 0)
    if pos.state is PositionState.PENDING_ENTRY:
        return _decide_pending_entry(pos, live_order, broker_qty,
                                     in_entry_window)
    if pos.state is PositionState.ENTERED:
        return _decide_entered(pos, broker_qty)
    if pos.state is PositionState.EXITING:
        return _decide_exiting(pos, live_order, broker_qty)
    if pos.state is PositionState.EXIT_FAILED:
        return _decide_exit_failed(pos, broker_qty)
    return None  # CLOSED/ENTRY_FAILED는 미종결 조회에 없음(방어)


def _decide_pending_entry(pos: TradePosition, live_order: str | None,
                          broker_qty: int,
                          in_entry_window: bool) -> ReconcileAction:
    symbol = pos.symbol
    if live_order is not None:
        if pos.entry_phase is not EntryPhase.CANCEL_REQUESTED and in_entry_window:
            # ② 미체결 진입 주문 생존 + 창 안 → 감시 재개
            return ReconcileAction(ReconcileKind.RESUME_ENTRY_WATCH, symbol,
                                   watch_order_no=live_order,
                                   note="② entry order alive — resume watch")
        # 창 밖(취소만 — 시장가 재발주 금지 §6-6.3) 또는 취소 미완(의도 유지):
        # 취소 후 잔고 기준 확정 — 부분 체결분이 있으면 ENTERED(잔고 수량)
        settled = (_entered_with(pos, broker_qty) if broker_qty > 0
                   else _entry_failed(pos))
        why = ("cancel was in-flight" if pos.entry_phase is
               EntryPhase.CANCEL_REQUESTED else "outside entry window")
        return ReconcileAction(ReconcileKind.CANCEL_AND_SETTLE_ENTRY, symbol,
                               position=settled, cancel_order_no=live_order,
                               note=f"② {why} — cancel, settle by balance "
                                    f"(qty={broker_qty})")
    if broker_qty > 0:
        # ① 주문 소멸 + 보유 → 체결 완료(시장가 미확정도 여기로 흡수)
        return ReconcileAction(ReconcileKind.PROMOTE_ENTERED, symbol,
                               position=_entered_with(pos, broker_qty),
                               note=f"① filled — ENTERED qty={broker_qty}")
    # 주문도 보유도 없음: ③ 고아 취소(CANCEL_REQUESTED) 또는 소멸·미체결
    label = ("③ orphan cancel" if pos.entry_phase is
             EntryPhase.CANCEL_REQUESTED else "entry order gone, no holdings")
    return ReconcileAction(ReconcileKind.FAIL_ENTRY, symbol,
                           position=_entry_failed(pos), alarm=True,
                           note=f"{label} — ENTRY_FAILED (no market re-entry: "
                                "signal window passed)")


def _decide_entered(pos: TradePosition, broker_qty: int) -> ReconcileAction | None:
    if broker_qty == 0:
        # ⑥-b 외부 처분 추정 — ENTERED로 두면 없는 물량에 매도를 시도한다
        return ReconcileAction(
            ReconcileKind.CLOSE, pos.symbol,
            position=replace(pos, state=PositionState.CLOSED), alarm=True,
            note="⑥-b ENTERED but broker holds none — external disposal "
                 "assumed, CLOSED (pnl unresolved, manual audit)")
    if broker_qty != pos.quantity:
        return ReconcileAction(
            ReconcileKind.REWATCH, pos.symbol,
            position=replace(pos, quantity=broker_qty), alarm=True,
            note=f"⑥-b quantity mismatch db={pos.quantity} "
                 f"broker={broker_qty} — aligned to balance, resume watch")
    return None  # 정합 — 조정 불요


def _decide_exiting(pos: TradePosition, live_order: str | None,
                    broker_qty: int) -> ReconcileAction:
    symbol = pos.symbol
    if live_order is not None:
        stale_limit = pos.exit_phase in (ExitPhase.LIMIT_SUBMITTED,
                                         ExitPhase.CANCEL_REQUESTED)
        if stale_limit and broker_qty > 0:
            # ⑤ 익절 지정가 생존(취소 미완 CANCEL_REQUESTED 포함 — 트레이더
            # P5-T6c C1: 취소 의도가 있던 스테일 지정가를 "추적"으로 살리면
            # 그 종목이 손절/트레일링 재평가에서 완전히 배제된다. 진입측
            # CANCEL_REQUESTED의 "취소 의도 유지"와 대칭) → 취소 후 감시
            # 복귀 — 다음 poll_once가 남은 타임아웃 무시하고 즉시 재평가
            # (사유 유효 시 §6-2-b 재집행). 수량은 잔고 ground truth.
            return ReconcileAction(
                ReconcileKind.CANCEL_AND_REWATCH, symbol,
                position=_rewatch(pos, broker_qty),
                cancel_order_no=live_order,
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
    # ⑤-b 주문 없음 + 보유 잔존(고아 취소·부분 청산·주문 소멸) → 잔고 수량으로
    # ENTERED 복귀, 감시가 재평가(청산은 신호 시점 제약 없음 — ③과 다름)
    return ReconcileAction(
        ReconcileKind.REWATCH, symbol, position=_rewatch(pos, broker_qty),
        note=f"⑤-b no exit order, still holding {broker_qty} — resume watch")


def _decide_exit_failed(pos: TradePosition, broker_qty: int) -> ReconcileAction:
    # live_order를 참조하지 않는 이유: KRX 정규장 주문은 전부 당일 유효(Day)라
    # EXIT_FAILED로 고정된 전일 주문이 재기동 시점에 잔존할 수 없다(트레이더
    # Minor — GTC/시간외 주문을 지원하게 되면 이 전제를 재검토할 것).
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


def _entered_with(pos: TradePosition, qty: int) -> TradePosition:
    return replace(pos, state=PositionState.ENTERED, entry_phase=None,
                   quantity=qty)


def _entry_failed(pos: TradePosition) -> TradePosition:
    return replace(pos, state=PositionState.ENTRY_FAILED, entry_phase=None)


def _rewatch(pos: TradePosition, qty: int) -> TradePosition:
    return replace(pos, state=PositionState.ENTERED, exit_phase=None,
                   exit_reason=None, quantity=qty)


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
      ⚠️ 취소는 성공했는데 영속만 실패한 액션은 applied에 **없다** — 브로커
      측 취소는 이미 확정이므로 소비자는 applied만으로 "아무 일도 없었다"고
      판단하지 말 것. warnings가 사유를 보존한다(아키텍트 Minor).
    - RESUME_* / WARN은 여기서 I/O 없음 — Task 7이 감시 재개/노출을 담당."""
    applied: list[ReconcileAction] = []
    warnings: list[str] = []
    for action in actions:
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
                warnings.append(
                    f"{action.symbol}: reconcile cancel failed for order "
                    f"{action.cancel_order_no} ({type(exc).__name__}) — "
                    "position state unchanged, rerun/manual required")
                logger.error("reconcile cancel failed %s (order_no=%s): %s",
                             action.symbol, action.cancel_order_no, exc)
                continue
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
                continue
        applied.append(action)
    return applied, warnings
