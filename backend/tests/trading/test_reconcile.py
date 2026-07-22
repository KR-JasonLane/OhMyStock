"""reconcile(6c) — §6-6 분기 ①~⑦(+⑤-b/⑥-b) 순수 전수 + apply 실패 정책 +
monitor pending 시드(재기동 고아 갭 봉쇄)."""

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.broker import Balance, OpenOrder, OrderSide, Position
from app.domain.trading.config import TradingConfig
from app.domain.trading.models import (EntryPhase, ExitPhase, ExitReason,
                                       PositionState, TradePosition)
from app.domain.trading.monitor import PositionMonitor
from app.domain.trading.reconcile import (DbPosition, ReconcileKind,
                                          apply_reconcile, reconcile_decide)
from tests.trading.conftest import (KST, FakeCalendar, FakeOrderPortBase,
                                    FakeStore)

T0 = datetime(2026, 7, 22, 9, 10, tzinfo=KST)

MON_CFG = TradingConfig(max_single_order_krw=100_000_000, max_daily_orders=100,
                        max_daily_order_krw=500_000_000,
                        min_avg_trading_value_krw=0,
                        exit_limit_timeout_sec=3.0, poll_interval_sec=1.0,
                        quote_failure_threshold=2)


async def _no_sleep(_):
    return None


def _seed_monitor(fake, store) -> PositionMonitor:
    """reconcile 시드 검증용 최소 monitor(conftest fake 조립 — 테스트 모듈 간
    직접 import 결합 금지, 개발자 P5-T6c #4)."""
    return PositionMonitor(fake, MON_CFG, FakeCalendar(), lambda *_: None,
                           store.persist, sleep=_no_sleep, now=lambda: T0)


def _pos(symbol="005930", state=PositionState.PENDING_ENTRY,
         entry_phase=None, exit_phase=None, exit_reason=None,
         qty=10) -> TradePosition:
    return TradePosition(symbol=symbol, name="테스트", market="kospi",
                         state=state, entry_price=100_000, quantity=qty,
                         peak_price=100_000, trailing_active=False,
                         entered_at=T0, entry_phase=entry_phase,
                         exit_phase=exit_phase, exit_reason=exit_reason)


def _balance(*holdings: tuple[str, int]) -> Balance:
    positions = tuple(
        Position(symbol=s, name="테스트", quantity=q, avg_price=100_000,
                 current_price=100_000, eval_amount=100_000 * q)
        for s, q in holdings)
    return Balance(positions=positions, total_eval=0, total_profit=0)


def _open(order_no: str, symbol="005930") -> OpenOrder:
    return OpenOrder(order_no=order_no, symbol=symbol, side=OrderSide.BUY,
                     order_qty=10, unfilled_qty=10, order_price=100_000,
                     status="접수")


def _decide_one(db, orders=(), balance=None, in_window=True):
    actions = reconcile_decide([db], list(orders),
                               balance or _balance(), in_window)
    return actions


# ── 진입 계열 (①②③) ─────────────────────────────────────────────────────

def test_분기1_주문소멸_보유있음은_ENTERED_승격():
    db = DbPosition(_pos(entry_phase=EntryPhase.LIMIT_SUBMITTED), ("ORD1",))
    [a] = _decide_one(db, balance=_balance(("005930", 7)))
    assert a.kind is ReconcileKind.PROMOTE_ENTERED
    assert a.position.state is PositionState.ENTERED
    assert a.position.quantity == 7          # 수량은 잔고 ground truth
    assert a.position.entry_phase is None


def test_분기1은_시장가_미확정도_흡수():
    db = DbPosition(_pos(entry_phase=EntryPhase.MARKET_SUBMITTED), ("ORD1",))
    [a] = _decide_one(db, balance=_balance(("005930", 10)))
    assert a.kind is ReconcileKind.PROMOTE_ENTERED


def test_분기2_진입주문_생존_창안은_감시재개():
    db = DbPosition(_pos(entry_phase=EntryPhase.LIMIT_SUBMITTED), ("ORD1",))
    [a] = _decide_one(db, orders=[_open("ORD1")], in_window=True)
    assert a.kind is ReconcileKind.RESUME_ENTRY_WATCH
    assert a.watch_order_no == "ORD1" and a.position is None


def test_분기2_창밖은_취소만_시장가_재발주_금지():
    db = DbPosition(_pos(entry_phase=EntryPhase.LIMIT_SUBMITTED), ("ORD1",))
    [a] = _decide_one(db, orders=[_open("ORD1")], in_window=False)
    assert a.kind is ReconcileKind.CANCEL_AND_SETTLE_ENTRY
    assert a.cancel_order_no == "ORD1"
    assert a.position.state is PositionState.ENTRY_FAILED


def test_분기2_창밖_부분체결은_취소_후_잔고수량_ENTERED():
    # kind는 액션 형태(취소 후 잔고 확정), 성패는 position.state가 말한다
    # (개발자 #2 — "FAIL" 명명과 ENTERED 결과의 모순 해소)
    db = DbPosition(_pos(entry_phase=EntryPhase.LIMIT_SUBMITTED), ("ORD1",))
    [a] = _decide_one(db, orders=[_open("ORD1")],
                      balance=_balance(("005930", 4)), in_window=False)
    assert a.kind is ReconcileKind.CANCEL_AND_SETTLE_ENTRY
    assert a.position.state is PositionState.ENTERED
    assert a.position.quantity == 4


def test_취소미완_주문생존은_창안에서도_취소_의도_유지():
    db = DbPosition(_pos(entry_phase=EntryPhase.CANCEL_REQUESTED), ("ORD1",))
    [a] = _decide_one(db, orders=[_open("ORD1")], in_window=True)
    assert a.kind is ReconcileKind.CANCEL_AND_SETTLE_ENTRY


def test_분기3_고아취소는_ENTRY_FAILED_알람():
    db = DbPosition(_pos(entry_phase=EntryPhase.CANCEL_REQUESTED), ("ORD1",))
    [a] = _decide_one(db)
    assert a.kind is ReconcileKind.FAIL_ENTRY and a.alarm is True
    assert a.position.state is PositionState.ENTRY_FAILED


def test_취소요청_후_체결분_발견은_ENTERED_승격():
    db = DbPosition(_pos(entry_phase=EntryPhase.CANCEL_REQUESTED), ("ORD1",))
    [a] = _decide_one(db, balance=_balance(("005930", 3)))
    assert a.kind is ReconcileKind.PROMOTE_ENTERED
    assert a.position.quantity == 3


def test_주문연결은_주문번호_명시_매칭_symbol_매칭_아님():
    # 같은 심볼의 남의 주문(ORD9 미보유)이 살아있어도 내 주문(ORD1)이 아니면
    # 생존으로 판정하지 않는다(§6-6.② — 개발자 델타 이월)
    db = DbPosition(_pos(entry_phase=EntryPhase.LIMIT_SUBMITTED), ("ORD1",))
    [a] = _decide_one(db, orders=[_open("ORD9")])
    assert a.kind is ReconcileKind.FAIL_ENTRY  # 주문·보유 없음 취급


# ── 보유 계열 (⑥-b) ──────────────────────────────────────────────────────

def test_ENTERED_정합은_무조정():
    db = DbPosition(_pos(state=PositionState.ENTERED))
    assert _decide_one(db, balance=_balance(("005930", 10))) == []


def test_분기6b_ENTERED인데_무보유는_외부처분_CLOSED_알람():
    db = DbPosition(_pos(state=PositionState.ENTERED))
    [a] = _decide_one(db)
    assert a.kind is ReconcileKind.CLOSE and a.alarm is True
    assert a.position.state is PositionState.CLOSED


def test_분기6b_수량_불일치는_잔고로_정합_후_감시():
    db = DbPosition(_pos(state=PositionState.ENTERED))
    [a] = _decide_one(db, balance=_balance(("005930", 6)))
    assert a.kind is ReconcileKind.REWATCH and a.alarm is True
    assert a.position.quantity == 6


# ── 청산 계열 (④⑤⑤-b⑦) ────────────────────────────────────────────────

def test_분기4_청산완료는_CLOSED():
    db = DbPosition(_pos(state=PositionState.EXITING,
                         exit_reason=ExitReason.STOP_LOSS), ("ORD2",))
    [a] = _decide_one(db)
    assert a.kind is ReconcileKind.CLOSE
    assert a.position.state is PositionState.CLOSED


def test_분기5_익절지정가_생존은_취소_후_감시복귀():
    db = DbPosition(_pos(state=PositionState.EXITING,
                         exit_phase=ExitPhase.LIMIT_SUBMITTED,
                         exit_reason=ExitReason.TAKE_PROFIT), ("ORD2",))
    [a] = _decide_one(db, orders=[_open("ORD2")],
                      balance=_balance(("005930", 10)))
    assert a.kind is ReconcileKind.CANCEL_AND_REWATCH
    assert a.cancel_order_no == "ORD2"
    assert a.position.state is PositionState.ENTERED  # 다음 폴이 즉시 재평가
    assert a.position.exit_phase is None and a.position.exit_reason is None


def test_분기5_시장가_청산_생존은_취소금지_추적재개():
    # 손절 시장가(ExitPhase 없음) — 6b "매도 취소 금지" 계약 유지
    db = DbPosition(_pos(state=PositionState.EXITING,
                         exit_reason=ExitReason.STOP_LOSS), ("ORD2",))
    [a] = _decide_one(db, orders=[_open("ORD2")],
                      balance=_balance(("005930", 10)))
    assert a.kind is ReconcileKind.RESUME_EXIT_WATCH
    assert a.watch_order_no == "ORD2" and a.cancel_order_no is None
    assert a.position is None  # RESUME 계열은 영속 없음(개발자 #1)


def test_취소미완_익절지정가_생존은_취소_재시도():
    """트레이더 P5-T6c C1 — CANCEL_REQUESTED(취소 의도가 있던 스테일 지정가)를
    추적으로 살리면 그 종목이 손절/트레일링 재평가에서 배제된다. 진입측과
    대칭으로 취소를 재시도하고 감시 복귀."""
    db = DbPosition(_pos(state=PositionState.EXITING,
                         exit_phase=ExitPhase.CANCEL_REQUESTED,
                         exit_reason=ExitReason.TAKE_PROFIT), ("ORD2",))
    [a] = _decide_one(db, orders=[_open("ORD2")],
                      balance=_balance(("005930", 10)))
    assert a.kind is ReconcileKind.CANCEL_AND_REWATCH
    assert a.cancel_order_no == "ORD2"
    assert a.position.state is PositionState.ENTERED


def test_분기5_지정가_생존인데_잔고0은_취소없이_추적():
    """개발자 #5 — 취소+수량 0 복귀는 불가능한 상태(전량 체결 직후 전파 지연
    등 모호). 추적 위임 — pending의 소멸=체결 확인(연속 2회)이 해소."""
    db = DbPosition(_pos(state=PositionState.EXITING,
                         exit_phase=ExitPhase.LIMIT_SUBMITTED,
                         exit_reason=ExitReason.TAKE_PROFIT), ("ORD2",))
    [a] = _decide_one(db, orders=[_open("ORD2")])
    assert a.kind is ReconcileKind.RESUME_EXIT_WATCH
    assert a.position is None


def test_취소미완_지정가_생존_잔고0도_취소없이_추적():
    # stale_limit(CANCEL_REQUESTED)이라도 잔고 0이면 취소 재시도 대신 추적
    # (아키텍트 #4 — 조합 전수의 회귀 고정)
    db = DbPosition(_pos(state=PositionState.EXITING,
                         exit_phase=ExitPhase.CANCEL_REQUESTED,
                         exit_reason=ExitReason.TAKE_PROFIT), ("ORD2",))
    [a] = _decide_one(db, orders=[_open("ORD2")])
    assert a.kind is ReconcileKind.RESUME_EXIT_WATCH
    assert a.cancel_order_no is None


def test_분기5_익절_시장가_폴백_생존도_취소금지():
    db = DbPosition(_pos(state=PositionState.EXITING,
                         exit_phase=ExitPhase.MARKET_SUBMITTED,
                         exit_reason=ExitReason.TAKE_PROFIT), ("ORD3",))
    [a] = _decide_one(db, orders=[_open("ORD3")],
                      balance=_balance(("005930", 10)))
    assert a.kind is ReconcileKind.RESUME_EXIT_WATCH


def test_분기5b_취소요청_후_주문없음_보유잔존은_감시복귀():
    db = DbPosition(_pos(state=PositionState.EXITING,
                         exit_phase=ExitPhase.CANCEL_REQUESTED,
                         exit_reason=ExitReason.TAKE_PROFIT), ("ORD2",))
    [a] = _decide_one(db, balance=_balance(("005930", 3)))
    assert a.kind is ReconcileKind.REWATCH
    assert a.position.state is PositionState.ENTERED
    assert a.position.quantity == 3  # 부분 체결 반영 — 잔고 ground truth


def test_분기7_EXIT_FAILED_무보유는_CLOSED_확정():
    db = DbPosition(_pos(state=PositionState.EXIT_FAILED,
                         exit_reason=ExitReason.STOP_LOSS))
    [a] = _decide_one(db)
    assert a.kind is ReconcileKind.CLOSE
    assert a.position.state is PositionState.CLOSED


def test_분기7_EXIT_FAILED_보유잔존은_자동재청산_금지_경고만():
    db = DbPosition(_pos(state=PositionState.EXIT_FAILED,
                         exit_reason=ExitReason.STOP_LOSS))
    [a] = _decide_one(db, balance=_balance(("005930", 10)))
    assert a.kind is ReconcileKind.WARN and a.alarm is True
    assert a.position is None  # 상태 변경 없음 — 수동 개입


def test_분기6_DB에_없는_브로커_보유는_경고():
    actions = reconcile_decide([], [], _balance(("000660", 5)), True)
    [a] = actions
    assert a.kind is ReconcileKind.WARN and a.alarm is True
    assert a.symbol == "000660"


# ── apply (부수효과 정책) ────────────────────────────────────────────────

@pytest.mark.anyio
async def test_apply_취소성공은_영속과_감사_수행():
    db = DbPosition(_pos(entry_phase=EntryPhase.LIMIT_SUBMITTED), ("ORD1",))
    actions = _decide_one(db, orders=[_open("ORD1")], in_window=False)
    fake = FakeOrderPortBase()
    persisted, recorded = [], []
    applied, warnings = await apply_reconcile(
        actions, fake, persisted.append,
        record_cancel=lambda ack, orig: recorded.append(orig))
    assert fake.cancelled == ["ORD1"]
    assert [p.state for p in persisted] == [PositionState.ENTRY_FAILED]
    assert recorded == ["ORD1"]
    assert len(applied) == 1 and warnings == []


@pytest.mark.anyio
async def test_apply_취소실패는_상태를_바꾸지_않고_경고():
    db = DbPosition(_pos(entry_phase=EntryPhase.LIMIT_SUBMITTED), ("ORD1",))
    actions = _decide_one(db, orders=[_open("ORD1")], in_window=False)
    fake = FakeOrderPortBase(cancel_script=[RuntimeError("rejected")])
    persisted = []
    applied, warnings = await apply_reconcile(actions, fake, persisted.append)
    assert persisted == []       # 주문 상태 불명 — 영속 금지
    assert applied == []
    assert any("cancel failed" in w for w in warnings)


@pytest.mark.anyio
async def test_apply_영속실패는_경고_후_다음_액션_계속():
    a1 = DbPosition(_pos(state=PositionState.EXIT_FAILED))            # ⑦ CLOSE
    a2 = DbPosition(_pos(symbol="000660", state=PositionState.EXIT_FAILED))
    actions = reconcile_decide([a1, a2], [], _balance(), True)
    fails = {"005930"}

    def persist(pos):
        if pos.symbol in fails:
            raise RuntimeError("db down")

    applied, warnings = await apply_reconcile(actions, FakeOrderPortBase(),
                                              persist)
    assert [a.symbol for a in applied] == ["000660"]  # 한 건 실패가 비전파
    assert any("persist failed" in w for w in warnings)


@pytest.mark.anyio
async def test_apply_알람은_구조화_필드로_경고_승격():
    db = DbPosition(_pos(entry_phase=EntryPhase.CANCEL_REQUESTED))  # ③
    actions = _decide_one(db)
    applied, warnings = await apply_reconcile(actions, FakeOrderPortBase(),
                                              lambda _: None)
    assert len(applied) == 1
    assert any("ENTRY_FAILED" in w for w in warnings)  # ③ 알람 노출


@pytest.mark.anyio
async def test_apply_취소_감사_실패는_경고로_노출():
    """보안 P5-T6c #1 — 취소는 나갔는데 감사 기록 실패가 로그에만 남으면
    로그 유실 시 재구성 불가. 상태 API 경고로 승출, 예외 원문은 미노출."""
    db = DbPosition(_pos(entry_phase=EntryPhase.LIMIT_SUBMITTED), ("ORD1",))
    actions = _decide_one(db, orders=[_open("ORD1")], in_window=False)
    fake = FakeOrderPortBase()

    def bad_audit(ack, orig):
        raise RuntimeError("db-dsn://user:pw@host down")

    applied, warnings = await apply_reconcile(actions, fake, lambda _: None,
                                              record_cancel=bad_audit)
    assert fake.cancelled == ["ORD1"] and len(applied) == 1  # 흐름은 계속
    audit_warning = next(w for w in warnings if "audit" in w)
    assert "RuntimeError" in audit_warning and "db-dsn" not in audit_warning


@pytest.mark.anyio
async def test_apply_RESUME_계열은_IO_없이_통과():
    db = DbPosition(_pos(state=PositionState.EXITING,
                         exit_reason=ExitReason.STOP_LOSS), ("ORD2",))
    actions = _decide_one(db, orders=[_open("ORD2")],
                          balance=_balance(("005930", 10)))
    fake = FakeOrderPortBase()
    persisted = []
    applied, warnings = await apply_reconcile(actions, fake, persisted.append)
    assert fake.cancelled == [] and fake.calls == []
    assert persisted == []  # RESUME 계열은 영속도 없음(개발자 #1 — I/O 없음)
    assert applied[0].kind is ReconcileKind.RESUME_EXIT_WATCH


# ── monitor pending 시드 (재기동 고아 갭 봉쇄 — 보안 P5-T6b #4) ─────────

@pytest.mark.anyio
async def test_track_existing_exit는_pending을_복원해_다음_폴이_확정():
    """시드된 pending은 미관측(seen_alive=False) — 부재=체결 판정에 연속
    2회 확인을 요구한다(보안 P5-T6c #2: 오시드/전파 지연이 실보유를 CLOSED로
    오판해 감시 밖 방치하는 것 방지)."""
    exiting = _pos(state=PositionState.EXITING,
                   exit_reason=ExitReason.STOP_LOSS)
    store = FakeStore(exiting)
    fake = FakeOrderPortBase(open_orders_script=[None, None])
    mon = _seed_monitor(fake, store)
    mon.track_existing_exit(exiting, "ORD7", est_price=99_000)
    acts1 = await mon.poll_once([], T0)
    assert acts1 == []  # 첫 부재 — 체결/오시드 구분 불가, CLOSED 금지
    acts2 = await mon.poll_once([], T0)  # 연속 2회 부재 — 체결 확정
    assert acts2[0].state is PositionState.CLOSED
    assert acts2[0].reason is ExitReason.STOP_LOSS
    assert acts2[0].requires_reconcile is True  # 지연 확정 — 잔고 대사 표식
    assert store.rows["005930"].state is PositionState.CLOSED
    assert acts2[0].exit_price == 99_000  # est 명시 시에만 추정 기록


@pytest.mark.anyio
async def test_시드된_pending은_첫_부재에서_유지된다():
    """미관측 pending의 1회 부재 = 체결/미전파/오시드 구분 불가 — 추적을
    유지하고 CLOSED를 만들지 않는다(연속 2회 확인은 위 테스트가 고정)."""
    exiting = _pos(state=PositionState.EXITING,
                   exit_reason=ExitReason.STOP_LOSS)
    store = FakeStore(exiting)
    fake = FakeOrderPortBase(open_orders_script=[None])
    mon = _seed_monitor(fake, store)
    mon.track_existing_exit(exiting, "ORD7", est_price=99_000)
    acts = await mon.poll_once([], T0)
    assert acts == [] and "005930" in mon._pending  # 오판 CLOSED 없음
    assert store.rows["005930"].state is PositionState.EXITING  # 상태 불변


@pytest.mark.anyio
async def test_시드_est_없으면_pnl을_확정_숫자로_기록하지_않는다():
    """트레이더 P5-T6c I4 — 신뢰할 추정 없이 entry_price 따위로 pnl을 계산해
    영속하면 손실이 과소평가된 '확정처럼 보이는' 숫자가 남는다. None 영속 +
    잔고 대사 강제 표식."""
    exiting = _pos(state=PositionState.EXITING,
                   exit_reason=ExitReason.STOP_LOSS)
    store = FakeStore(exiting)
    fake = FakeOrderPortBase(open_orders_script=[None, None])
    mon = _seed_monitor(fake, store)
    mon.track_existing_exit(exiting, "ORD7")  # est_price 미상
    await mon.poll_once([], T0)
    acts = await mon.poll_once([], T0)
    assert acts[0].state is PositionState.CLOSED
    assert acts[0].exit_price is None and acts[0].realized_pnl is None
    assert acts[0].requires_reconcile is True
    assert store.rows["005930"].realized_pnl is None
