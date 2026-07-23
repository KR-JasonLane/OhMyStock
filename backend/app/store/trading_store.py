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

from dataclasses import dataclass
from datetime import date

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import sessionmaker

from app.store.kst_time import (as_aware_utc, coarse_utc_bounds,
                                within_kst_day)

from app.domain.trading.models import (EntryPhase, ExitPhase, ExitReason,
                                       PositionState, TradePosition)
from app.store.models import (CandleRow, InstrumentRow, TradeFillRow,
                              TradeOrderRow, TradePositionRow, TradeRunRow)

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


@dataclass(frozen=True)
class DailyOrderUsage:
    """당일(KST 거래일) 주문 사용량 — 같은 날 재기동 run의 OrderCaps 시딩
    입력(P6 스펙 §5-1, P5 정정). has_buy는 진입 배치 게이트 입력(당일 매수
    발주가 이미 있으면 재기동 run은 진입 배치를 건너뛴다)."""
    order_count: int
    order_krw: int
    has_buy: bool


@dataclass(frozen=True)
class EntryContext:
    """entry_context 반환 행 — selection.EntryCandidate의 저장소측 재료
    (current_price는 브로커 시세라 서비스가 별도 조인)."""
    symbol: str
    name: str
    market: str
    audit_info: str
    state: str
    signal_price: int          # 기준일 종가 (0 = 결측 — selection이 제외)
    avg_trading_value_krw: int


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

    def create_run(self, config_json: str,
                   run_environment: str = "mock") -> int:
        # run_environment: §4-1 감사 분리(mock/real/replay) — 리플레이 런의
        # 가짜 체결이 집계에 섞이지 않게 하는 구조적 필터(Alembic 0009)
        with self._sessions.begin() as session:
            run = TradeRunRow(started_at=self._now(), status="running",
                              config=config_json,
                              run_environment=run_environment)
            session.add(run)
            session.flush()
            return run.id

    def foreign_open_position_count(self, run_environment: str) -> int:
        """지정 환경 **밖**의 런에 속한 미종결 포지션 수(트레이더 R6
        Critical) — 리플레이 프로필 기동 fail-fast 입력: 같은 DB에 실전/
        모의 미종결 포지션이 남아 있으면 리플레이 reconcile이 그것을
        '브로커 미보유 → CLOSED'로 오판해 TP/SL 감시에서 이탈시킨다."""
        with self._sessions() as session:
            rows = session.execute(
                select(TradePositionRow.id)
                .join(TradeRunRow,
                      TradePositionRow.trade_run_id == TradeRunRow.id)
                .where(TradePositionRow.state.in_(_OPEN_STATES))
                .where(TradeRunRow.run_environment != run_environment)
            ).scalars().all()
        return len(rows)

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

    def daily_order_usage(self, day: date,
                          run_environment: str) -> DailyOrderUsage:
        """당일(KST) 주문 집계 — 같은 날 재기동 run의 일일 한도(OrderCaps)
        시딩(P6 스펙 §5-1). **매수·매도 구분 없이 합산**한다 —
        `OrderCaps.check()`가 side 무관 누적 후 SELL만 차단을 면제하는
        의미론과 일치시켜, 재기동 복원값이 연속 실행 대비 느슨해지지 않게
        한다(P6 계획 리뷰 개발자 지적).

        - run_environment 조인 필터: 리플레이 주문이 모의 한도를 소비하는
          (또는 그 역의) 교차 오염 차단 — open_positions와 동일 근거(§4-1).
        - 금액은 주문당 `max(est_krw, req_price×req_qty, 체결 합)`:
          **시장가는 req_price=0이고 record_fill이 프로덕션 미배선**(P6-T1
          트레이더 Critical — 체결 합은 현재 항상 0)이라, 발주 시점 caps
          추정 금액(est_krw — OrderRequest.ref_price 유래)이 시장가 금액의
          유일한 원천이다. max는 과계상 방향(§8-1 "선누적 과계상 — 보수
          방향" 관례). record_fill이 배선되면 체결 합이 자연히 반영된다.
        - 취소 감사 행 제외(`status='cancelled' AND updated_at IS NULL`):
          취소는 세 경로 모두(reconcile `_record_reconcile_cancel`, 진입
          지정가 타임아웃 entry.py, 청산 지정가 타임아웃 monitor.py) 기존
          행 전이가 아니라 **새 행을 status='cancelled'로 삽입**하며(P6-T1
          개발자 #3 교차 확인), 그 행들은 caps.check 카운트 대상(발주)이
          아니다. updated_at을 남기는 `update_order_status`는 현재 프로덕션
          미호출 — 사실상 "모든 cancelled 행 제외"와 동치다. ⚠️ TODO: 취소를
          in-place 전이(update_order_status)로 리팩터링하는 날에는 이 필터를
          함께 재설계할 것(발주된 지정가가 취소 전이되며 통째로 빠지는
          역방향 결함 — 원 발주 행은 카운트에 남아야 한다).
        - 날짜 판정은 KST 변환 후 파이썬에서 정확 비교(P6 계획 Task 4
          Critical과 동일 클래스 — created_at은 UTC 저장이라 SQL DATE
          비교는 08:20 KST 시작 런을 전날로 오분류한다). SQL은 ±1일 여유의
          거친 범위로만 프리필터(스캔 상한 — 주문은 일 수십 건 수준)."""
        coarse_lo, coarse_hi = coarse_utc_bounds(day)
        count = 0
        krw = 0
        has_buy = False
        with self._sessions() as session:
            rows = session.execute(
                select(TradeOrderRow)
                .join(TradeRunRow,
                      TradeOrderRow.trade_run_id == TradeRunRow.id)
                .where(TradeRunRow.run_environment == run_environment,
                       TradeOrderRow.created_at >= coarse_lo,
                       TradeOrderRow.created_at <= coarse_hi)
            ).scalars().all()
            for row in rows:
                if not within_kst_day(row.created_at, day):
                    continue
                if row.status == "cancelled" and row.updated_at is None:
                    continue  # reconcile 취소 감사 행 — 발주 아님
                fills = session.execute(
                    select(func.coalesce(
                        func.sum(TradeFillRow.fill_price
                                 * TradeFillRow.fill_qty), 0))
                    .where(TradeFillRow.order_id == row.id)).scalar_one()
                count += 1
                krw += max(row.est_krw, row.req_price * row.req_qty,
                           int(fills))
                if row.side == "buy":
                    has_buy = True
        return DailyOrderUsage(order_count=count, order_krw=krw,
                               has_buy=has_buy)

    # ---------- P6 스케줄러 판정 헬퍼 (read-only, 스펙 §6) ----------

    def has_completed_run(self, reference_date: date,
                          run_environment: str) -> bool:
        """해당 KST 날짜에 시작한 완료 run 존재. **완료 = succeeded OR
        (stopped AND stopped_by_kill_switch)** — 셧다운 취소도 'stopped'로
        기록되므로(service.py CancelledError 경로) 컬럼 검사를 빼면 장중
        크래시 재기동이 "완료"로 오판돼 캐치업이 무력화된다(스펙 §4-d,
        계획 리뷰 개발자 Critical). 킬스위치 stopped만 완료(같은 날 자동
        재기동 금지 — 운영자 의사 존중).

        run_environment: **필수 인자(기본값 없음 — P6-T4 보안 Important:
        daily_order_usage와 동일 관례)** — 리플레이 런이 모의/실전 스케줄의
        "오늘 몫"으로 오판되는 교차 오염 차단(4개 스토어 중 유일한 시그니처
        편차 — 문서화)."""
        for row in self._day_runs(reference_date, run_environment):
            if row.status == "succeeded" or (
                    row.status == "stopped" and row.stopped_by_kill_switch):
                return True
        return False

    def last_failed_finished_at(self, reference_date: date,
                                run_environment: str) -> datetime | None:
        """실패 = failed OR (stopped AND NOT kill_switch — 셧다운/크래시
        취소도 재시도 백오프 대상, §4-d 미완료 분기)의 마지막 종료 시각."""
        stamps = [
            as_aware_utc(row.finished_at)
            for row in self._day_runs(reference_date, run_environment)
            if row.finished_at is not None
            and (row.status == "failed"
                 or (row.status == "stopped"
                     and not row.stopped_by_kill_switch))]
        return max(stamps, default=None)

    def _day_runs(self, day: date, run_environment: str) -> list[TradeRunRow]:
        lo, hi = coarse_utc_bounds(day)
        with self._sessions() as session:
            rows = session.scalars(
                select(TradeRunRow)
                .where(TradeRunRow.run_environment == run_environment,
                       TradeRunRow.started_at >= lo,
                       TradeRunRow.started_at <= hi)).all()
            return [row for row in rows
                    if within_kst_day(row.started_at, day)]

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

    def submitted_order_nos(self, position_id: int) -> tuple[str, ...]:
        """포지션에 연결된 미종결(submitted) 주문번호 — reconcile ②의 명시
        연결 입력(§6-6: symbol 매칭 금지, trade_position_id로만 판단)."""
        with self._sessions() as session:
            rows = session.scalars(
                select(TradeOrderRow.order_no)
                .where(TradeOrderRow.trade_position_id == position_id,
                       TradeOrderRow.status == "submitted")).all()
            return tuple(rows)

    def entry_context(self, symbols: list[str], signal_date: date,
                      avg_days: int = 20) -> dict[str, "EntryContext"]:
        """진입 후보 조인(§6-3 — selection 입력 재료). collection 소유 테이블
        (instruments/candles)의 **읽기 전용** 조회다 — 트레이딩은 이 데이터를
        쓰지 않는다(경계 주석, 단일 엔진 전제).

        signal_price = signal_date(스코어링 기준일)의 종가 — P3 as-of 신호가
        (갭 가드 비교 기준). avg_trading_value = 최근 avg_days개 일봉의
        close×volume 평균(스펙 §6-3.2 — 별도 TR 없이 수집 자산 재사용)."""
        if not symbols:
            return {}
        out: dict[str, EntryContext] = {}
        with self._sessions() as session:
            instruments = {
                row.symbol: row for row in session.scalars(
                    select(InstrumentRow)
                    .where(InstrumentRow.symbol.in_(symbols))).all()}
            signal_rows = dict(session.execute(
                select(CandleRow.symbol, CandleRow.close)
                .where(CandleRow.symbol.in_(symbols),
                       CandleRow.date == signal_date)).all())
            for symbol in symbols:
                inst = instruments.get(symbol)
                if inst is None:
                    continue  # 카탈로그에 없는 픽 — 호출자가 결측 경고
                recent = session.scalars(
                    select(CandleRow).where(CandleRow.symbol == symbol,
                                            CandleRow.date <= signal_date)
                    .order_by(CandleRow.date.desc()).limit(avg_days)).all()
                avg_value = (int(sum(c.close * c.volume for c in recent)
                                 / len(recent)) if recent else 0)
                out[symbol] = EntryContext(
                    symbol=symbol, name=inst.name, market=inst.market,
                    audit_info=inst.audit_info, state=inst.state,
                    signal_price=int(signal_rows.get(symbol, 0)),
                    avg_trading_value_krw=avg_value)
        return out

    def get_position(self, position_id: int) -> TradePosition | None:
        """상태 무관 단건 조회 — CLOSED 직후 하드 게이트 재오픈(P5-T7 트레이더
        C3: open_positions는 CLOSED를 제외하므로 방금 닫힌 행의 market을 여기서
        읽는다)."""
        with self._sessions() as session:
            row = session.get(TradePositionRow, position_id)
            return _row_to_position(row) if row is not None else None

    def recent_closed_symbols(self, cutoff: datetime) -> set[str]:
        """cutoff 이후 CLOSED 확정된 심볼 — 재진입 쿨다운 입력(§8-1
        reentry_cooldown_min, P5-T7 트레이더 I6). DB 기반이라 당일 재기동
        후에도 쿨다운이 유지된다."""
        with self._sessions() as session:
            rows = session.scalars(
                select(TradePositionRow.symbol).distinct()
                .where(TradePositionRow.state == PositionState.CLOSED.value,
                       TradePositionRow.closed_at.is_not(None),
                       TradePositionRow.closed_at >= cutoff)).all()
            return set(rows)

    def instrument_state(self, symbol: str) -> str | None:
        """종목 state 원문(ka10099 — 수집 시점 스냅샷, 읽기 전용) — monitor의
        거래정지 vs 네트워크 구분(§6-4)에 사용. ⚠️ 최신성은 마지막 수집
        시점까지다(장중 신규 정지는 반영 전일 수 있음 — 구분 실패 시 monitor는
        네트워크 의심 경고로 폴백하므로 보수 방향)."""
        with self._sessions() as session:
            return session.scalar(select(InstrumentRow.state)
                                  .where(InstrumentRow.symbol == symbol))

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

    def open_positions(self, run_environment: str | None = None,
                       ) -> tuple[list[tuple[int, TradePosition]], list[int]]:
        """미종결 포지션(reconcile §6-6 입력, EXIT_FAILED 포함 — 스펙 분기 ⑦).
        반환: (정상 [(position_id, TradePosition)], 오염 position_id 목록).

        run_environment(트레이더 R6 Critical): 지정 시 해당 환경의 런에
        속한 포지션만 반환 — 리플레이 프로세스가 같은 DB의 실전/모의
        포지션을 읽어 reconcile로 CLOSED 처리(감시 이탈)하는 교차 오염을
        구조적으로 차단한다(§4-1 감사 컬럼의 실소비 지점).

        enum 역직렬화 실패(손상 행)를 행 단위로 격리한다(아키텍트 T5) — 오염
        1건이 전체 목록 조회를 죽이면 정상 N−1개까지 감시 밖으로 밀려나
        "미종결을 잃지 않는다"는 최우선 계약과 정면 충돌. 오염 행은 error
        로그 + id 반환으로 표면화하며 호출자(6c)가 §6-7 warnings에 노출한다."""
        good: list[tuple[int, TradePosition]] = []
        corrupted: list[int] = []
        with self._sessions() as session:
            query = (select(TradePositionRow)
                     .where(TradePositionRow.state.in_(_OPEN_STATES))
                     .order_by(TradePositionRow.id))
            if run_environment is not None:
                query = query.join(
                    TradeRunRow,
                    TradePositionRow.trade_run_id == TradeRunRow.id,
                ).where(TradeRunRow.run_environment == run_environment)
            rows = session.execute(query).scalars().all()
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
                     req_qty: int, status: str, resp_body: dict,
                     est_krw: int = 0) -> int:
        """주문 이력. resp_body는 브로커 응답 **바디만**(§9) — 타입·민감 키를
        런타임 검증(_validate_resp_body — 헤더 dict/토큰 문자열의 실수 유입을
        fail-loud로 차단, 보안 패널)한 뒤 JSON 직렬화.

        est_krw: 발주 시점 caps.check 추정 금액(P6-T1) — 시장가는 req_price=0
        이라 이 값이 재기동 시딩(daily_order_usage)의 금액 원천이다. 감사
        전용 행(reconcile 취소 등 발주 아님)은 0 유지."""
        _validate_resp_body(resp_body)
        with self._sessions.begin() as session:
            row = TradeOrderRow(
                trade_run_id=run_id, trade_position_id=position_id,
                order_no=order_no, symbol=symbol, side=side,
                order_style=order_style, req_price=req_price, req_qty=req_qty,
                status=status, est_krw=est_krw,
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
