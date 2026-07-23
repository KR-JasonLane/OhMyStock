"""TradingService — P5 트레이딩 엔진 통합 오케스트레이션(계획서 Task 7).

책임: 순수/부수 모듈(selection·entry·monitor·reconcile — Task 3/6a/6b/6c)을
TradingStore·브로커 포트와 배선하고 실행 수명주기(§6-5 정지 계약·§8-1 버그
봉쇄·§9 감사)를 관리한다. 도메인 모듈은 이 서비스가 주입하는 콜백으로만
store를 만난다(Global Constraints — store 통짜 주입 금지).

핵심 계약(패널 이월 — 계획서 Task 7 스텝과 1:1):
- persist_position은 store.save_position_snapshot(None=비움)으로 배선 —
  update_position(None=미변경)과 혼용 금지(P5-T6c 아키텍트 #2).
- caps `(amount, side)` — 차단은 **매수 전용**, 매도(청산·킬스위치)는 기록만
  (P5-T6b 트레이더 C2: 리스크 축소 주문이 자기 안전장치에 막히는 역설 방지).
- requires_reconcile=True → **즉시** 미니 reconcile(잔고 대사 — 재기동 대기
  금지, P5-T6a 트레이더 I2). CLOSED는 잔고 교차 검증 후에만 최종 확정 —
  잔고 잔존 시 재오픈(하드 게이트, P5-T6c 보안 #2).
- EntryOutcome(position=None, requires_reconcile=True)은 확정 ENTRY_FAILED로
  영속하지 않는다(§6-3.8 캐비어트 — 미니 reconcile이 최종 상태 결정).
- PositionMonitor는 trade_run당 새 인스턴스 + 단일 루프 순차 호출(P5-T6b
  아키텍트 #5).
- 진입 직후 잔고 대사로 수량·평단 확정, 잔고 0이면 유령 포지션 즉시 해소
  (6a C1 방어선 ⓒ).

블로킹 콜백 트레이드오프(계획서 to_thread 노트의 실구현 해석): 도메인 콜백
(persist/on_order/caps)은 **동기 계약**(fail-closed persist는 발주 전 완료가
전제라 비동기화 불가)이므로 store 호출이 이벤트 루프를 짧게 블로킹한다 —
로컬 DB 단건 트랜잭션(수 ms)으로 한정되며, 서비스 레벨의 대량 I/O(런 생성·
목록 조회·finish)는 asyncio.to_thread로 감싼다."""

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from app.core.background_service import BackgroundRunService, StopMode
from app.domain.broker import BrokerPort, OrderPort, OrderSide
from app.domain.trading.config import TradingConfig
from app.domain.trading.entry import EntryExecutor, EntryOutcome
from app.domain.trading.models import PositionState, TradePosition
from app.domain.trading.monitor import ExitAction, PositionMonitor
from app.domain.trading.reconcile import (DbPosition, ReconcileKind,
                                          apply_reconcile, reconcile_decide)
from app.domain.trading.selection import (EntryCandidate, EntryPlan,
                                          select_entries)

logger = logging.getLogger(__name__)


class _StoreLike(Protocol):
    """서비스가 소비하는 TradingStore 표면(테스트 fake 계약)."""

    def create_run(self, config_json: str,
                   run_environment: str = "mock") -> int: ...
    def finish_run(self, run_id: int, status: str,
                   stopped_by_kill_switch: bool = False,
                   kill_switch_mode: str | None = None,
                   failure_reason: str | None = None) -> None: ...
    def create_position(self, run_id: int, position: TradePosition) -> int: ...
    def update_position(self, position_id: int, **kwargs) -> None: ...
    def save_position_snapshot(self, position_id: int,
                               pos: TradePosition) -> None: ...
    def open_positions(self, run_environment: str | None = None): ...
    def submitted_order_nos(self, position_id: int) -> tuple[str, ...]: ...
    def record_order(self, run_id, position_id, order_no, symbol, side,
                     order_style, req_price, req_qty, status, resp_body): ...
    def entry_context(self, symbols, signal_date): ...
    def instrument_state(self, symbol: str) -> str | None: ...
    def get_position(self, position_id: int) -> TradePosition | None: ...
    def recent_closed_symbols(self, cutoff: datetime) -> set[str]: ...


class SingleOrderCapExceeded(ValueError):
    """단건 상한 위반 — 해당 후보만의 문제(전역 소진 아님). 진입 배치는 이
    후보만 스킵하고 계속한다(P5-T7 트레이더 I5 — 뭉뚱그린 배치 중단은 정상
    후보의 하루치 진입 기회를 잃는다)."""


class DailyCapExceeded(ValueError):
    """일일 건수/금액 상한 소진 — 전역 래치(buy_blocked). 신규 진입 배치 중단."""


class OrderCaps:
    """§8-1 버그 봉쇄 한도 구현체 — Task 6a/6b가 주입받는 check(amount, side).

    매수(BUY): 단건 상한(SingleOrderCapExceeded — 후보 단위)·일일 건수/금액
    (DailyCapExceeded — 전역 래치) 초과 시 발주 차단 — 사이징 버그의 첫 주문을
    발주 직전에 잡는 마지막 방어선. 일일 상한 도달 시 buy_blocked 래치 —
    신규 진입만 정지(§8-1 "엔진 자동 정지"의 범위).
    매도(SELL): **절대 차단하지 않는다**(P5-T6b 트레이더 C2/아키텍트 #4 —
    킬스위치·손절이 자기 안전장치에 막히는 역설 방지). 기록/카운트만.
    카운트는 발주 직전 선누적(발주 실패 시 과계상 — 보수 방향. 시장가 상방
    슬리피지의 체계적 과소계상 여부는 잔고 사후 보정 검토 — 계획서 노트)."""

    def __init__(self, config: TradingConfig) -> None:
        self._config = config
        self.order_count = 0
        self.order_krw = 0
        self.buy_blocked = False

    def check(self, amount_krw: int, side: OrderSide) -> None:
        self.order_count += 1
        self.order_krw += amount_krw
        if side is OrderSide.SELL:
            return
        if self.buy_blocked:
            raise DailyCapExceeded(
                "daily order caps exhausted — new entries stopped")
        if amount_krw > self._config.max_single_order_krw:
            raise SingleOrderCapExceeded(
                f"single order cap exceeded: {amount_krw} > "
                f"{self._config.max_single_order_krw}")
        if (self.order_count > self._config.max_daily_orders
                or self.order_krw > self._config.max_daily_order_krw):
            self.buy_blocked = True
            raise DailyCapExceeded(
                "daily order cap exceeded — new entries stopped")


@dataclass(frozen=True)
class TradingProgress:
    """GET /trade/status 계약(§6-7). started_at/finished_at은 베이스 서비스
    타임스탬프(4서비스 공통)를 API 계층이 합성한다."""
    run_id: int | None
    status: str                      # running | stopping | succeeded | failed | stopped
    positions_count: int
    warnings: tuple[str, ...]
    daily_order_count: int
    daily_order_krw: int
    kill_switch: str | None          # 요청된 정지 모드(없으면 None)


class TradingService(BackgroundRunService):
    """상시 루프 서비스(§6-4) — 사이클 경계에서 stop_requested() 확인(§6-5:
    주문 발행 같은 원자 구간에서는 신호를 무시하고 완료 후 반영)."""

    def __init__(self, orders: OrderPort, account: BrokerPort,
                 store: _StoreLike, config: TradingConfig, calendar,
                 analysis_latest, conflict_check=None,
                 sleep=None, now=None,
                 run_environment: str = "mock") -> None:
        """calendar: core.market_calendar 모듈(주입 — 6b와 동일 이유).
        analysis_latest: () -> dict | None (AnalysisStore.latest_results —
        동기, to_thread 경유 호출).
        run_environment: mock/real/replay — trade_runs 감사 컬럼(§4-1,
        조립부가 Settings.run_environment에서 유도해 전달)."""
        super().__init__("trading", conflict_check=conflict_check,
                         logger=logger, now=now)  # 4서비스 타임스탬프 대칭
        self._orders = orders
        self._account = account
        self._store = store
        self._config = config
        self._run_environment = run_environment
        self._calendar = calendar
        self._analysis_latest = analysis_latest
        self._sleep = sleep or asyncio.sleep
        self._clock = now or (lambda: datetime.now(timezone.utc))
        self._run_id: int | None = None
        self._pos_ids: dict[str, int] = {}
        self._caps: OrderCaps | None = None
        self._monitor: PositionMonitor | None = None
        self._warnings: list[str] = []
        self._entries_done = False
        self._final_status = "idle"

    # ── 상태 노출 ───────────────────────────────────────────────────────

    def progress(self) -> TradingProgress:
        caps = self._caps
        mode = self.stop_requested()
        if self.is_running():
            status = "stopping" if mode is not None else "running"
        else:
            status = self._final_status
        monitor_warnings = tuple(self._monitor.warnings) if self._monitor else ()
        return TradingProgress(
            run_id=self._run_id, status=status,
            positions_count=len(self._pos_ids),
            warnings=tuple(self._warnings) + monitor_warnings,
            daily_order_count=caps.order_count if caps else 0,
            daily_order_krw=caps.order_krw if caps else 0,
            kill_switch=mode.value if mode else None)

    def _on_accepted(self) -> None:
        # 필드 대입만(베이스 계약 — 예외 금지)
        self._run_id = None
        self._pos_ids = {}
        self._warnings = []
        self._entries_done = False
        self._final_status = "running"
        self._caps = OrderCaps(self._config)
        # run당 새 인스턴스(P5-T6b 아키텍트 #5 — 거래일 경계 재사용 금지)
        self._monitor = PositionMonitor(
            self._orders, self._config, self._calendar, self._caps.check,
            persist_position=self._persist_by_symbol,
            on_order=self._record_order_by_symbol,
            lookup_instrument_state=self._lookup_state,
            sleep=self._sleep, now=self._clock)

    # ── 실행 본문 ───────────────────────────────────────────────────────

    async def _run(self) -> None:
        self._run_id = await asyncio.to_thread(
            self._store.create_run, self._config.to_json(),
            self._run_environment)
        status, failure = "succeeded", None
        try:
            await self._reconcile_startup()
            await self._trading_loop()
            if self.stop_requested() is not None:
                status = "stopped"
        except asyncio.CancelledError:
            # 강제 취소(셧다운/재배포) — "성공"으로 오기록 금지(보안 P5-T7 #1,
            # scoring 서비스 관례와 동일). finally의 finish_run이 기록한다.
            status, failure = "stopped", "cancelled (shutdown)"
            raise
        except Exception as exc:  # noqa: BLE001 — 실패는 기록으로 표면화
            # failure_reason은 타입명만(예외 원문은 로그 전용 — P5-T6b #3
            # 위생 계약: DB 드라이버 예외 원문은 자격증명 포함 가능)
            status, failure = "failed", type(exc).__name__
            logger.exception("trading run failed")
        finally:
            # 모든 종료 경로에서 finish_run 보장(§9 — status='running' 좀비
            # 행은 감사 질문에 답하지 못한다. P5-T5 보안 이월)
            mode = self.stop_requested()
            self._final_status = status
            try:
                await asyncio.to_thread(
                    self._store.finish_run, self._run_id, status,
                    stopped_by_kill_switch=mode is not None,
                    kill_switch_mode=mode.value if mode else None,
                    failure_reason=failure)
            except Exception:  # noqa: BLE001
                logger.exception("finish_run failed for run %s", self._run_id)

    async def _trading_loop(self) -> None:
        while True:
            now = self._clock()
            mode = self.stop_requested()
            if mode is StopMode.LIQUIDATE_ALL:
                await self._liquidate_all(now)
                return
            if not self._calendar.is_market_hours(now):
                return  # 15:30 정상 반환(§6-4) — 다음 거래일은 새 run
            if (mode is None and not self._entries_done
                    and not self._caps.buy_blocked
                    and self._in_entry_window(now)):
                self._entries_done = True  # 진입은 run당 1회 배치(§6-3)
                await self._enter_positions(now)
            positions = await asyncio.to_thread(self._load_entered)
            actions = await self._monitor.poll_once(positions, now)
            await self._post_actions(actions)
            await self._sleep(self._monitor.recommended_delay(now))

    # ── 재기동 reconcile (§6-6) ─────────────────────────────────────────

    async def _reconcile_startup(self) -> None:
        rows, corrupted = await asyncio.to_thread(
            self._store.open_positions, self._run_environment)
        for pid in corrupted:
            self._warnings.append(
                f"position row {pid} corrupted (enum) — excluded from "
                "reconcile, manual repair required")
        self._pos_ids = {pos.symbol: pid for pid, pos in rows}
        positions = {pos.symbol: pos for _pid, pos in rows}
        db_positions = []
        for pid, pos in rows:
            order_nos = await asyncio.to_thread(
                self._store.submitted_order_nos, pid)
            db_positions.append(DbPosition(pos, order_nos))
        applied, open_orders = await self._run_reconcile(db_positions,
                                                         positions)
        live_prices = {o.order_no: o.order_price for o in open_orders}
        settled = False
        for action in applied:
            if action.kind is ReconcileKind.RESUME_ENTRY_WATCH:
                await self._resume_entry(positions[action.symbol],
                                         action.watch_order_no,
                                         live_prices.get(action.watch_order_no, 0))
            elif action.kind in (ReconcileKind.CANCEL_AND_SETTLE_ENTRY,
                                 ReconcileKind.CANCEL_AND_REWATCH):
                settled = True
        if settled:
            # 취소~적용 사이 추가 체결 레이스 — 잔고 재조회로 최종 수량 재확정
            # (P5-T6c 트레이더 I3)
            await self._align_open_with_balance()

    async def _run_reconcile(self, db_positions, positions):
        """decide→apply→경고 취합→pos_ids 정합→RESUME_EXIT 시드 — 재기동/
        미니 reconcile 공통 골격(개발자 P5-T7 #3 중복 제거). 브로커 상태를
        못 읽으면 예외 전파(블라인드 기동 금지 — run 실패로 표면화)."""
        open_orders = await self._orders.get_open_orders()
        balance = await self._account.get_balance()
        actions = reconcile_decide(db_positions, open_orders, balance,
                                   self._in_entry_window(self._clock()))
        applied, warnings = await apply_reconcile(
            actions, self._orders, self._persist_by_symbol,
            record_cancel=self._record_reconcile_cancel)
        self._warnings.extend(warnings)
        for action in applied:
            # 종결 판정(CLOSE/FAIL_ENTRY)은 보유 집합에서 즉시 제거(개발자
            # P5-T7 Critical #1 — 방치 시 그 심볼 재진입이 막히고 stale
            # 항목이 max_positions 슬롯·positions_count를 오염)
            if (action.position is not None
                    and action.position.state in (PositionState.CLOSED,
                                                  PositionState.ENTRY_FAILED)):
                self._pos_ids.pop(action.symbol, None)
            if action.kind is ReconcileKind.RESUME_EXIT_WATCH:
                self._monitor.track_existing_exit(positions[action.symbol],
                                                  action.watch_order_no)
        return applied, open_orders

    async def _resume_entry(self, pos: TradePosition, order_no: str,
                            limit_price: int) -> None:
        """② 진입 지정가 생존(창 안) — EntryExecutor.resume으로 꼬리 재개."""
        pos_id = self._pos_ids[pos.symbol]
        plan = EntryPlan(symbol=pos.symbol, name=pos.name, market=pos.market,
                        quantity=pos.quantity,
                        budget_krw=pos.entry_price * pos.quantity)
        quotes = await self._orders.get_quotes([pos.symbol])
        ask = quotes[0].ask if quotes and quotes[0].ask > 0 else pos.entry_price
        if limit_price <= 0:
            limit_price = pos.entry_price
        outcome = await self._executor_for(pos_id).resume(
            plan, ask, order_no, limit_price)
        await self._apply_entry_outcome(plan, pos_id, outcome)

    # ── 진입 (§6-3) ─────────────────────────────────────────────────────

    async def _enter_positions(self, now: datetime) -> None:
        latest = await asyncio.to_thread(self._analysis_latest)
        if not latest or not latest.get("picks"):
            self._warnings.append("no analysis picks — entries skipped")
            return
        signal_date = date.fromisoformat(latest["score_reference_date"])
        expected = self._last_trading_day_before(now)
        if signal_date != expected:
            # 신호 낡음(§6-3) 또는 **미래 신호**(트레이더 R6 #3 — 리플레이
            # 앵커가 과거일 때 실시계 분석 픽이 통과하면 재생 시점에 존재
            # 하지 않던 정보로 진입하는 look-ahead. 프로덕션에서도 미래
            # 신호는 데이터 손상 신호다). 양방향 정확 일치만 통과.
            self._warnings.append(
                f"analysis signal date mismatch (signal {signal_date}, "
                f"expected {expected}) — entries skipped")
            return
        symbols = [p["symbol"] for p in latest["picks"]]
        context = await asyncio.to_thread(self._store.entry_context, symbols,
                                          signal_date)
        quotes = {md.quote.symbol: md
                  for md in await self._orders.get_quotes(symbols)}
        deposit = await self._account.get_deposit()
        held = set(self._pos_ids)
        candidates = []
        for symbol in symbols:  # pick rank 순서 유지
            ctx = context.get(symbol)
            md = quotes.get(symbol)
            if ctx is None or md is None:
                self._warnings.append(
                    f"{symbol}: pick missing context/quote — skipped")
                continue
            candidates.append(EntryCandidate(
                symbol=symbol, name=ctx.name, market=ctx.market,
                signal_price=ctx.signal_price, current_price=md.quote.price,
                audit_info=ctx.audit_info, state=ctx.state,
                avg_trading_value_krw=ctx.avg_trading_value_krw))
        # 재진입 쿨다운(§8-1 reentry_cooldown_min — 트레이더 I6): 최근 청산
        # 심볼은 후보에서 제외. DB 기반이라 당일 재기동에도 유지된다.
        cutoff = self._clock() - timedelta(
            minutes=self._config.reentry_cooldown_min)
        cooldown = await asyncio.to_thread(self._store.recent_closed_symbols,
                                           cutoff)
        for symbol in sorted(cooldown & {c.symbol for c in candidates}):
            self._warnings.append(f"{symbol}: reentry cooldown — skipped")
        candidates = [c for c in candidates if c.symbol not in cooldown]

        selection = select_entries(candidates, held, deposit.available,
                                   self._config)
        # 탈락 사유 표면화(Task 8 라이브 결함 수정 — 침묵 드랍이 "왜 안
        # 사는가"를 40분간 가렸다): warnings(API 노출) + 상세 로그(결정 #36)
        for drop in selection.dropped:
            self._warnings.append(
                f"{drop.symbol}: entry dropped — {drop.reason}")
            logger.warning("entry candidate dropped: %s — %s",
                           drop.symbol, drop.reason)
        plans = selection.plans
        for plan in plans:
            if self.stop_requested() is not None:
                return  # 정지 신호 — 신규 진입 중단(진행 주문은 없음)
            # 발주 직전 시세 재조회(트레이더 I4) — 선순위 후보의 체결 대기
            # (최대 ~2분)로 배치 초 스냅샷이 낡는다. 실패 시 스냅샷 폴백.
            md = quotes[plan.symbol]
            try:
                fresh = await self._orders.get_quotes([plan.symbol])
                if fresh:
                    md = fresh[0]
            except Exception:  # noqa: BLE001
                self._warnings.append(
                    f"{plan.symbol}: pre-entry requote failed — using batch "
                    "snapshot")
            ask = md.ask if md.ask > 0 else md.quote.price
            pending = TradePosition(
                symbol=plan.symbol, name=plan.name, market=plan.market,
                state=PositionState.PENDING_ENTRY,
                entry_price=md.quote.price, quantity=plan.quantity,
                peak_price=md.quote.price, trailing_active=False)
            pos_id = await asyncio.to_thread(self._store.create_position,
                                             self._run_id, pending)
            self._pos_ids[plan.symbol] = pos_id
            try:
                outcome = await self._executor_for(pos_id).execute(plan, ask)
            except SingleOrderCapExceeded as exc:
                # 후보 단위 위반(트레이더 I5) — 이 후보만 스킵, 배치 계속
                self._warnings.append(
                    f"{plan.symbol}: entry blocked by single-order cap "
                    f"({exc}) — skipped, batch continues")
                await asyncio.to_thread(
                    self._store.update_position, pos_id,
                    state=PositionState.ENTRY_FAILED)
                del self._pos_ids[plan.symbol]
                continue
            except DailyCapExceeded as exc:
                # 전역 소진(§8-1) — 신규 진입 배치 중단
                self._warnings.append(f"entry batch stopped by daily cap: "
                                      f"{exc}")
                await asyncio.to_thread(
                    self._store.update_position, pos_id,
                    state=PositionState.ENTRY_FAILED)
                del self._pos_ids[plan.symbol]
                return
            await self._apply_entry_outcome(plan, pos_id, outcome)

    async def _apply_entry_outcome(self, plan: EntryPlan, pos_id: int,
                                   outcome: EntryOutcome) -> None:
        symbol = plan.symbol
        if outcome.position is not None:
            entered = replace(outcome.position, entered_at=self._clock())
            await asyncio.to_thread(self._store.save_position_snapshot,
                                    pos_id, entered)
            await self._verify_entry_with_balance(pos_id, entered)
            if outcome.requires_reconcile:
                await self._mini_reconcile(symbol)
            return
        if outcome.requires_reconcile:
            # ⚠️ 확정 ENTRY_FAILED 금지(§6-3.8 캐비어트 — 마지막 EntryPhase
            # 유지) — 미니 reconcile이 브로커 ground truth로 최종 상태 결정
            self._warnings.append(
                f"{symbol}: entry unresolved ({outcome.failure_reason}) — "
                "mini reconcile")
            await self._mini_reconcile(symbol)
            return
        await asyncio.to_thread(self._store.update_position, pos_id,
                                state=PositionState.ENTRY_FAILED)
        self._pos_ids.pop(symbol, None)
        self._warnings.append(f"{symbol}: entry failed — "
                              f"{outcome.failure_reason}")

    async def _verify_entry_with_balance(self, pos_id: int,
                                         entered: TradePosition) -> None:
        """진입 직후 잔고 대사(kt00018) — 수량·평단을 실측으로 확정하고 잔고
        결측이면 유령 포지션 해소(6a C1 방어선 ⓒ).

        유령 판정은 **유예 후 재조회 포함 2회 확인**(트레이더 P5-T7 C2):
        kt00018 반영이 체결 확인 시점과 동기라는 실측은 없다 — 단발 스냅샷
        결측으로 실체결 포지션을 비가역 CLOSED(미종결 스캔 제외) 처리하면
        그 주식은 영구 무감시가 된다. 2회 연속 결측일 때만 CLOSED+알람."""
        broker_pos = None
        for attempt in (1, 2):
            balance = await self._account.get_balance()
            broker_pos = next((p for p in balance.positions
                               if p.symbol == entered.symbol
                               and p.quantity > 0), None)
            if broker_pos is not None:
                break
            if attempt == 1:  # 전파 유예 후 1회 재확인
                await self._sleep(self._config.poll_interval_sec * 2)
        if broker_pos is None:
            self._warnings.append(
                f"{entered.symbol}: entered but balance shows none "
                "(2 checks) — phantom position closed (alarm)")
            await asyncio.to_thread(
                self._store.save_position_snapshot, pos_id,
                replace(entered, state=PositionState.CLOSED,
                        closed_at=self._clock()))
            self._pos_ids.pop(entered.symbol, None)
            return
        if (broker_pos.quantity != entered.quantity
                or broker_pos.avg_price != entered.entry_price):
            corrected = replace(
                entered, quantity=broker_pos.quantity,
                entry_price=broker_pos.avg_price,
                peak_price=max(entered.peak_price, broker_pos.avg_price))
            await asyncio.to_thread(self._store.save_position_snapshot,
                                    pos_id, corrected)

    # ── 감시/청산 후처리 ────────────────────────────────────────────────

    async def _post_actions(self, actions: list[ExitAction]) -> None:
        for action in actions:
            if action.state is PositionState.CLOSED:
                # 쿨다운 근거는 DB closed_at(§8-1 — recent_closed_symbols)
                logger.info("position closed: %s (%s)", action.symbol,
                            action.reason.value)
        if not any(a.requires_reconcile for a in actions):
            self._forget_closed(actions)
            return
        balance = await self._account.get_balance()
        held = {p.symbol: p for p in balance.positions if p.quantity > 0}
        for action in actions:
            if not action.requires_reconcile:
                continue
            broker_pos = held.get(action.symbol)
            if (action.state is PositionState.CLOSED
                    and broker_pos is not None):
                # 하드 게이트(P5-T6c 보안 #2): CLOSED인데 잔고 잔존 —
                # 오판 확정. 재오픈해 감시로 복귀.
                pos_id = self._pos_ids.get(action.symbol)
                if pos_id is None:
                    # 방어선이 조용히 무력화되면 안 된다(개발자 P5-T7 #6)
                    logger.error("CLOSED-with-holdings for %s but no tracked "
                                 "position id — manual intervention",
                                 action.symbol)
                    self._warnings.append(
                        f"{action.symbol}: CLOSED but balance holds "
                        f"{broker_pos.quantity} and position id unknown — "
                        "manual intervention required")
                    continue
                # market은 방금 CLOSED로 영속된 원본 행에서(트레이더 C3 —
                # open_positions는 CLOSED 제외라 kospi 폴백 오분류가 났었다)
                origin = await asyncio.to_thread(self._store.get_position,
                                                 pos_id)
                reopened = TradePosition(
                    symbol=action.symbol, name=broker_pos.name,
                    market=origin.market if origin else "kospi",
                    state=PositionState.ENTERED,
                    entry_price=broker_pos.avg_price,
                    quantity=broker_pos.quantity,
                    peak_price=broker_pos.avg_price,
                    trailing_active=False, entered_at=self._clock())
                await asyncio.to_thread(self._store.save_position_snapshot,
                                        pos_id, reopened)
                self._warnings.append(
                    f"{action.symbol}: CLOSED overturned — balance still "
                    f"holds {broker_pos.quantity}, reopened for watch")
        self._forget_closed(actions)

    def _forget_closed(self, actions: list[ExitAction]) -> None:
        for action in actions:
            if action.state is PositionState.CLOSED:
                # 재오픈된 심볼은 위에서 snapshot이 ENTERED로 남으므로 여기서
                # 매핑을 지워도 다음 사이클 _load_entered가 다시 채운다
                self._pos_ids.pop(action.symbol, None)

    async def _liquidate_all(self, now: datetime) -> None:
        """킬스위치 LIQUIDATE_ALL(§8-1-b) — 보유 전량 시장가 청산 후 **전
        포지션이 CLOSED/EXIT_FAILED로 종결될 때까지 폴링 유지**(트레이더
        P5-T7 C1: 발행만 하고 반환하면 미체결 매도가 무감시로 방치 — 킬스위치
        가 가장 필요한 급락/VI 국면에서 정확히 무력화된다). 발행은 병렬
        (poll_once의 gather 원칙과 동일), 15:30 도달 시 §8-1-b대로 잔여를
        EXIT_FAILED로 강제 확정하고 반환한다."""
        first = True
        while True:
            now = self._clock()
            entered = await asyncio.to_thread(self._load_entered)
            if not entered and not self._monitor.has_pending:
                return  # 전 포지션 종결
            if not first and not self._calendar.is_market_hours(now):
                # 장 마감 — pending은 poll_once(_check_pending)가 EXIT_FAILED
                # 확정, 잔여 ENTERED(발주 실패 재시도분)는 강제 확정
                actions = await self._monitor.poll_once([], now)
                await self._post_actions(actions)
                for pos in entered:
                    pos_id = self._pos_ids.get(pos.symbol)
                    if pos_id is not None:
                        await asyncio.to_thread(
                            self._store.save_position_snapshot, pos_id,
                            replace(pos, state=PositionState.EXIT_FAILED))
                    self._warnings.append(
                        f"{pos.symbol}: liquidation incomplete at market "
                        "close — EXIT_FAILED (still held)")
                return
            first = False
            # ENTERED 잔여(최초 전량 + 이후 발주 실패 재시도분)는 재청산 지시
            # — 감시 재평가(poll_once의 exit_rules)가 아니라 킬스위치 강제
            liquidations = list(await asyncio.gather(
                *[self._monitor.liquidate(pos, now) for pos in entered]))
            liquidations += await self._monitor.poll_once([], now)  # pending 확인
            await self._post_actions(liquidations)
            await self._sleep(self._monitor.recommended_delay(now))

    # ── 배선 콜백 (도메인 → store) ──────────────────────────────────────

    def _persist_by_symbol(self, pos: TradePosition) -> None:
        pos_id = self._pos_ids.get(pos.symbol)
        if pos_id is None:
            raise ValueError(f"unknown position for persist: {pos.symbol}")
        self._store.save_position_snapshot(pos_id, pos)

    def _executor_for(self, pos_id: int) -> EntryExecutor:
        return EntryExecutor(
            self._orders, self._config, self._caps.check,
            persist_phase=lambda phase: self._store.update_position(
                pos_id, entry_phase=phase),
            on_order=self._record_order_for(pos_id),
            sleep=self._sleep, now=self._clock)

    def _record_order_for(self, pos_id: int | None):
        def record(ack, req, status: str) -> None:
            self._store.record_order(
                self._run_id, pos_id, order_no=ack.order_no,
                symbol=req.symbol, side=req.side.value,
                order_style=req.style.value, req_price=req.limit_price,
                req_qty=req.quantity, status=status,
                resp_body={"ord_no": ack.order_no, "return_msg": ack.message})
        return record

    def _record_order_by_symbol(self, ack, req, status: str) -> None:
        """monitor용 감사 — 청산 주문을 심볼로 포지션에 연결(개발자 Minor:
        동작에 맞는 명명 — _persist_by_symbol과 대칭)."""
        pos_id = self._pos_ids.get(req.symbol)
        self._record_order_for(pos_id)(ack, req, status)

    def _record_reconcile_cancel(self, ack, action) -> None:
        # 방향은 액션 유형에서 유추(진입 취소=buy, 청산 취소=sell — 보안
        # P5-T7 Minor: 하드코딩 심볼/방향은 감사 정확성 훼손)
        side = ("sell" if action.kind is ReconcileKind.CANCEL_AND_REWATCH
                else "buy")
        self._store.record_order(
            self._run_id, self._pos_ids.get(action.symbol),
            order_no=ack.order_no, symbol=action.symbol, side=side,
            order_style="limit", req_price=0, req_qty=1, status="cancelled",
            resp_body={"ord_no": ack.order_no,
                       "orig_ord_no": action.cancel_order_no,
                       "return_msg": ack.message})

    async def _lookup_state(self, symbol: str) -> str | None:
        return await asyncio.to_thread(self._store.instrument_state, symbol)

    # ── 미니 reconcile / 잔고 정합 ──────────────────────────────────────

    async def _mini_reconcile(self, symbol: str) -> None:
        """단일 심볼 즉시 대조(P5-T6a 트레이더 I2) — 재기동 reconcile과 같은
        골격(_run_reconcile)을 그 심볼의 DB 상태에만 적용한다."""
        rows, _corrupted = await asyncio.to_thread(
            self._store.open_positions, self._run_environment)
        target = [(pid, pos) for pid, pos in rows if pos.symbol == symbol]
        if not target:
            return
        pid, pos = target[0]
        order_nos = await asyncio.to_thread(self._store.submitted_order_nos,
                                            pid)
        await self._run_reconcile([DbPosition(pos, order_nos)],
                                  {pos.symbol: pos})

    async def _align_open_with_balance(self) -> None:
        """CANCEL_* 적용 직후 잔고 재확정(P5-T6c 트레이더 I3) — 취소와 체결이
        경합한 심볼의 수량을 잔고 ground truth로 갱신."""
        balance = await self._account.get_balance()
        held = {p.symbol: p.quantity for p in balance.positions}
        rows, _ = await asyncio.to_thread(
            self._store.open_positions, self._run_environment)
        for pid, pos in rows:
            if pos.state is not PositionState.ENTERED:
                continue
            broker_qty = held.get(pos.symbol, 0)
            if broker_qty > 0 and broker_qty != pos.quantity:
                await asyncio.to_thread(
                    self._store.save_position_snapshot, pid,
                    replace(pos, quantity=broker_qty))
                self._warnings.append(
                    f"{pos.symbol}: quantity aligned to balance "
                    f"({pos.quantity}→{broker_qty})")

    # ── 조회/시간 유틸 ──────────────────────────────────────────────────

    def _load_entered(self) -> list[TradePosition]:
        rows, _corrupted = self._store.open_positions(self._run_environment)
        self._pos_ids.update({pos.symbol: pid for pid, pos in rows})
        return [pos for _pid, pos in rows
                if pos.state is PositionState.ENTERED]

    def _in_entry_window(self, now: datetime) -> bool:
        kst = now.astimezone(self._calendar.KST)
        if not self._calendar.is_trading_day(kst.date()):
            return False
        return (self._config.entry_window_start <= kst.time()
                < self._config.entry_window_end)

    def _last_trading_day_before(self, now: datetime) -> date:
        d = now.astimezone(self._calendar.KST).date() - timedelta(days=1)
        while not self._calendar.is_trading_day(d):
            d -= timedelta(days=1)
        return d
