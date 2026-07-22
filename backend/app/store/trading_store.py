"""트레이딩 영속화·조회(P5 Task 5). 동기 SQLAlchemy — 서비스가 asyncio.to_thread
로 호출(AnalysisStore와 동일 패턴).

특성: 주문/체결은 insert-only(감사 자산 — 갱신은 orders.status/positions의 상태
전이만), 전 FK 비-CASCADE. resp_body는 응답 **바디만**(JSON 텍스트 — §9 보안).
open_positions()는 reconcile(§6-6)의 입력 — 재기동 시 미종결 포지션을 잃지 않는
것이 이 저장소의 최우선 계약."""

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import Engine, select
from sqlalchemy.orm import sessionmaker

from app.domain.trading.models import (EntryPhase, ExitPhase, ExitReason,
                                       PositionState, TradePosition)
from app.store.models import (TradeFillRow, TradeOrderRow, TradePositionRow,
                              TradeRunRow)

logger = logging.getLogger(__name__)

# resp_body 레드섹션(§9 보안 최종 방어선) — 이 저장소는 insert-only+비-CASCADE
# +삭제 API 부재라 한 번 새면 영구 잔존한다. 헤더 dict도 dict 타입이라 타입
# 힌트만으로는 못 막는다(보안 패널) — 민감 키가 보이면 즉시 거부.
_FORBIDDEN_BODY_KEYS = ("authorization", "token", "appkey", "secretkey", "secret")


def _validate_resp_body(resp_body: dict) -> dict:
    if not isinstance(resp_body, dict):
        raise TypeError(
            f"resp_body must be a response-body dict, got {type(resp_body).__name__}")
    for key in resp_body:
        lowered = str(key).lower()
        if any(f in lowered for f in _FORBIDDEN_BODY_KEYS):
            raise ValueError(
                f"resp_body contains credential-like key {key!r} — refusing to "
                "persist (audit rows are permanent)")
    return resp_body

# 미종결 상태(§6-6 reconcile 대상) — CLOSED/ENTRY_FAILED만 종결로 취급.
# EXIT_FAILED는 "실패로 고정됐지만 실보유가 남아있을 수 있는" 상태라 미종결에
# 포함한다(재기동 시 반드시 재확인 — 스펙 §6-1 침묵 금지).
_OPEN_STATES = (PositionState.PENDING_ENTRY.value, PositionState.ENTERED.value,
                PositionState.EXITING.value, PositionState.EXIT_FAILED.value)


def _row_to_position(row: TradePositionRow) -> TradePosition:
    return TradePosition(
        symbol=row.symbol, name=row.name, market=row.market,
        state=PositionState(row.state),
        entry_price=row.entry_price, quantity=row.quantity,
        peak_price=row.peak_price, trailing_active=row.trailing_active,
        entered_at=row.entered_at,
        entry_phase=EntryPhase(row.entry_phase) if row.entry_phase else None,
        exit_phase=ExitPhase(row.exit_phase) if row.exit_phase else None,
        exit_price=row.exit_price,
        exit_reason=ExitReason(row.exit_reason) if row.exit_reason else None,
        realized_pnl=row.realized_pnl, closed_at=row.closed_at)


class TradingStore:
    def __init__(self, engine: Engine,
                 now: Callable[[], datetime] | None = None) -> None:
        self._sessions = sessionmaker(bind=engine)
        self._now = now or (lambda: datetime.now(timezone.utc))

    # ---------- 런 ----------

    def create_run(self, config_json: str) -> int:
        with self._sessions.begin() as session:
            run = TradeRunRow(started_at=self._now(), status="running",
                              config=config_json)
            session.add(run)
            session.flush()
            return run.id

    def finish_run(self, run_id: int, status: str,
                   stopped_by_kill_switch: bool = False,
                   kill_switch_mode: str | None = None,
                   failure_reason: str | None = None) -> None:
        """킬 스위치 감사(§9 보안 패널) — Task 7이 _run() 종료 시
        request_stop 여부·모드로부터 기록한다."""
        with self._sessions.begin() as session:
            run = session.get(TradeRunRow, run_id)
            if run is None:
                raise ValueError(f"unknown trade run: {run_id}")
            run.finished_at = self._now()
            run.status = status
            run.stopped_by_kill_switch = stopped_by_kill_switch
            run.kill_switch_mode = kill_switch_mode
            run.failure_reason = failure_reason

    # ---------- 포지션 (상태 전이) ----------

    def create_position(self, run_id: int, position: TradePosition) -> int:
        with self._sessions.begin() as session:
            row = TradePositionRow(
                trade_run_id=run_id, symbol=position.symbol, name=position.name,
                market=position.market, state=position.state.value,
                entry_phase=(position.entry_phase.value
                             if position.entry_phase else None),
                exit_phase=(position.exit_phase.value
                            if position.exit_phase else None),
                entry_price=position.entry_price, quantity=position.quantity,
                peak_price=position.peak_price,
                trailing_active=position.trailing_active,
                entered_at=position.entered_at)
            session.add(row)
            session.flush()
            return row.id

    def update_position(self, position_id: int, *,
                        state: PositionState | None = None,
                        entry_phase: EntryPhase | None = None,
                        exit_phase: ExitPhase | None = None,
                        entry_price: int | None = None,
                        quantity: int | None = None,
                        peak_price: int | None = None,
                        trailing_active: bool | None = None,
                        exit_price: int | None = None,
                        exit_reason: ExitReason | None = None,
                        realized_pnl: int | None = None,
                        entered_at: datetime | None = None,
                        closed_at: datetime | None = None) -> None:
        """부분 갱신(상태 전이). None 인자는 **미변경** — phase/enum을 명시적으로
        비워야 하는 도메인 스냅샷 영속(PersistPosition 콜백)은
        `save_position_snapshot`을 쓸 것(P5-T6c 아키텍트 #2)."""
        with self._sessions.begin() as session:
            row = session.get(TradePositionRow, position_id)
            if row is None:
                raise ValueError(f"unknown trade position: {position_id}")
            if state is not None:
                row.state = state.value
            if entry_phase is not None:
                row.entry_phase = entry_phase.value
            if exit_phase is not None:
                row.exit_phase = exit_phase.value
            if entry_price is not None:
                row.entry_price = entry_price
            if quantity is not None:
                row.quantity = quantity
            if peak_price is not None:
                row.peak_price = peak_price
            if trailing_active is not None:
                row.trailing_active = trailing_active
            if exit_price is not None:
                row.exit_price = exit_price
            if exit_reason is not None:
                row.exit_reason = exit_reason.value
            if realized_pnl is not None:
                row.realized_pnl = realized_pnl
            if entered_at is not None:
                row.entered_at = entered_at
            if closed_at is not None:
                row.closed_at = closed_at

    def save_position_snapshot(self, position_id: int,
                               pos: TradePosition) -> None:
        """도메인 `PersistPosition` 콜백용 **전체 스냅샷 영속** — None 필드는
        '비움'으로 기록된다(update_position의 None=미변경과 다른 계약 —
        P5-T6c 아키텍트 #2: ENTERED 복귀·CLOSED 확정은 entry/exit_phase·
        exit_reason의 명시적 clear를 전제하며, 미변경으로 남기면 재기동
        reconcile이 스테일 phase를 보고 살아있는 시장가 청산 주문을
        오취소한다). 식별 필드(symbol/name/market/trade_run_id)는 불변 —
        갱신하지 않는다."""
        with self._sessions.begin() as session:
            row = session.get(TradePositionRow, position_id)
            if row is None:
                raise ValueError(f"unknown trade position: {position_id}")
            row.state = pos.state.value
            row.entry_phase = (pos.entry_phase.value
                               if pos.entry_phase is not None else None)
            row.exit_phase = (pos.exit_phase.value
                              if pos.exit_phase is not None else None)
            row.entry_price = pos.entry_price
            row.quantity = pos.quantity
            row.peak_price = pos.peak_price
            row.trailing_active = pos.trailing_active
            row.exit_price = pos.exit_price
            row.exit_reason = (pos.exit_reason.value
                               if pos.exit_reason is not None else None)
            row.realized_pnl = pos.realized_pnl
            row.entered_at = pos.entered_at
            row.closed_at = pos.closed_at

    def open_positions(self) -> tuple[list[tuple[int, TradePosition]], list[int]]:
        """미종결 포지션(reconcile §6-6 입력, EXIT_FAILED 포함 — 스펙 분기 ⑦).
        반환: (정상 [(position_id, TradePosition)], 오염 position_id 목록).

        enum 역직렬화 실패(손상 행)를 행 단위로 격리한다(아키텍트 T5) — 오염
        1건이 전체 목록 조회를 죽이면 정상 N−1개까지 감시 밖으로 밀려나
        "미종결을 잃지 않는다"는 최우선 계약과 정면 충돌. 오염 행은 error
        로그 + id 반환으로 표면화하며 호출자(6c)가 §6-7 warnings에 노출한다."""
        good: list[tuple[int, TradePosition]] = []
        corrupted: list[int] = []
        with self._sessions() as session:
            rows = session.execute(
                select(TradePositionRow)
                .where(TradePositionRow.state.in_(_OPEN_STATES))
                .order_by(TradePositionRow.id)).scalars().all()
            for row in rows:
                try:
                    good.append((row.id, _row_to_position(row)))
                except (ValueError, KeyError) as exc:
                    corrupted.append(row.id)
                    logger.error(
                        "trade_positions row %d corrupted (%s: %s) — excluded "
                        "from reconcile input, manual inspection required",
                        row.id, type(exc).__name__, exc)
        return good, corrupted

    # ---------- 주문/체결 (insert-only) ----------

    def record_order(self, run_id: int, position_id: int | None, order_no: str,
                     symbol: str, side: str, order_style: str, req_price: int,
                     req_qty: int, status: str, resp_body: dict) -> int:
        """주문 이력. resp_body는 브로커 응답 **바디만**(§9) — 타입·민감 키를
        런타임 검증(_validate_resp_body — 헤더 dict/토큰 문자열의 실수 유입을
        fail-loud로 차단, 보안 패널)한 뒤 JSON 직렬화."""
        _validate_resp_body(resp_body)
        with self._sessions.begin() as session:
            row = TradeOrderRow(
                trade_run_id=run_id, trade_position_id=position_id,
                order_no=order_no, symbol=symbol, side=side,
                order_style=order_style, req_price=req_price, req_qty=req_qty,
                status=status,
                resp_body=json.dumps(resp_body, ensure_ascii=False),
                created_at=self._now())
            session.add(row)
            session.flush()
            return row.id

    def update_order_status(self, order_id: int, status: str) -> None:
        with self._sessions.begin() as session:
            row = session.get(TradeOrderRow, order_id)
            if row is None:
                raise ValueError(f"unknown trade order: {order_id}")
            row.status = status
            row.updated_at = self._now()  # 전이 시각 — 감사 재구성(아키텍트 T5)

    def orders_for_position(self, position_id: int) -> list[TradeOrderRow]:
        """포지션→주문 명시 연결(개발자 델타 — reconcile ② symbol 매칭 금지)."""
        with self._sessions() as session:
            return list(session.execute(
                select(TradeOrderRow)
                .where(TradeOrderRow.trade_position_id == position_id)
                .order_by(TradeOrderRow.id)).scalars().all())

    def record_fill(self, order_id: int, fill_price: int, fill_qty: int,
                    filled_at: datetime) -> int:
        with self._sessions.begin() as session:
            row = TradeFillRow(order_id=order_id, fill_price=fill_price,
                               fill_qty=fill_qty, filled_at=filled_at)
            session.add(row)
            session.flush()
            return row.id

    # ---------- 조회 (API) ----------

    def latest_run(self) -> dict | None:
        """최근 런 요약 — /trade/status 보조(감사·킬스위치 노출)."""
        with self._sessions() as session:
            run = session.execute(
                select(TradeRunRow).order_by(TradeRunRow.id.desc())
                .limit(1)).scalar_one_or_none()
            if run is None:
                return None
            return {"run_id": run.id, "status": run.status,
                    "started_at": run.started_at.isoformat(),
                    "finished_at": (run.finished_at.isoformat()
                                    if run.finished_at else None),
                    "stopped_by_kill_switch": run.stopped_by_kill_switch,
                    "kill_switch_mode": run.kill_switch_mode,
                    "failure_reason": run.failure_reason}
