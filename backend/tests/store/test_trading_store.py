"""TradingStore(P5 Task 5) — 런/포지션 전이/주문·체결 기록/미종결 조회."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select

from app.domain.trading.models import (EntryPhase, ExitPhase, ExitReason,
                                       PositionState, TradePosition)
from app.store.models import Base, TradeOrderRow
from app.store.trading_store import TradingStore

T0 = datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def store(engine) -> TradingStore:
    return TradingStore(engine, now=lambda: T0)


def _pos(**overrides) -> TradePosition:
    base = dict(symbol="005930", name="삼성전자", market="kospi",
                state=PositionState.PENDING_ENTRY, entry_price=272_750,
                quantity=1, peak_price=272_750, trailing_active=False,
                entry_phase=EntryPhase.LIMIT_SUBMITTED)
    return TradePosition(**{**base, **overrides})


def test_런_생성과_킬스위치_감사_종료(store):
    run_id = store.create_run('{"max_positions": 5}')
    store.finish_run(run_id, "stopped", stopped_by_kill_switch=True,
                     kill_switch_mode="liquidate_all")
    latest = store.latest_run()
    assert latest["run_id"] == run_id and latest["status"] == "stopped"
    assert latest["stopped_by_kill_switch"] is True
    assert latest["kill_switch_mode"] == "liquidate_all"
    # sqlite는 tz-naive로 반환(Postgres는 tz 보존) — 프리픽스만 비교
    assert latest["started_at"].startswith("2026-07-22T00:00:00")


def test_미지_런_종료는_ValueError(store):
    with pytest.raises(ValueError, match="unknown trade run"):
        store.finish_run(999, "succeeded")


def test_포지션_생성_전이_왕복(store):
    run_id = store.create_run("{}")
    pos_id = store.create_position(run_id, _pos())
    # 체결 → ENTERED (trailing 상태 영속 — §6-2)
    store.update_position(pos_id, state=PositionState.ENTERED,
                          peak_price=280_000, trailing_active=True,
                          entered_at=T0)
    open_, corrupted = store.open_positions()
    assert len(open_) == 1 and corrupted == []
    pid, pos = open_[0]
    assert pid == pos_id and pos.state is PositionState.ENTERED
    assert pos.peak_price == 280_000 and pos.trailing_active is True
    # enum 왕복(문자열 저장 → enum 복원)이 정확한지 — reconcile 입력 계약
    assert pos.entry_phase is EntryPhase.LIMIT_SUBMITTED


def test_open_positions는_종결_상태를_제외하되_EXIT_FAILED는_포함(store):
    run_id = store.create_run("{}")
    p1 = store.create_position(run_id, _pos())
    p2 = store.create_position(run_id, _pos(symbol="000660", name="SK하이닉스"))
    p3 = store.create_position(run_id, _pos(symbol="035420", name="NAVER"))
    store.update_position(p1, state=PositionState.CLOSED,
                          exit_reason=ExitReason.STOP_LOSS, closed_at=T0)
    store.update_position(p2, state=PositionState.EXIT_FAILED)
    ids = [pid for pid, _ in store.open_positions()[0]]
    # CLOSED 제외, EXIT_FAILED는 실보유 잔존 가능이라 포함(§6-1 침묵 금지)
    assert ids == [p2, p3]


def test_주문_기록은_바디만_JSON으로(store, engine):
    run_id = store.create_run("{}")
    pos_id = store.create_position(run_id, _pos())
    order_id = store.record_order(
        run_id, pos_id, order_no="0034447", symbol="005930", side="buy",
        order_style="limit", req_price=246_000, req_qty=1, status="submitted",
        resp_body={"ord_no": "0034447", "return_msg": "모의투자 매수주문완료"})
    store.update_order_status(order_id, "cancelled")
    orders = store.orders_for_position(pos_id)
    assert len(orders) == 1 and orders[0].order_no == "0034447"
    assert orders[0].status == "cancelled"
    assert "Authorization" not in orders[0].resp_body  # 바디만(§9)


def test_체결_기록과_포지션_주문_연결(store):
    run_id = store.create_run("{}")
    pos_id = store.create_position(run_id, _pos())
    order_id = store.record_order(
        run_id, pos_id, order_no="0051385", symbol="005930", side="buy",
        order_style="market", req_price=0, req_qty=1, status="submitted",
        resp_body={})
    store.record_fill(order_id, fill_price=272_750, fill_qty=1, filled_at=T0)
    # 포지션→주문 명시 연결(reconcile ② — symbol 매칭 금지, 개발자 델타)
    assert store.orders_for_position(pos_id)[0].id == order_id


def test_포지션_미연결_주문도_기록_가능(store, engine):
    # 접수 실패 주문은 포지션 없이도 감사 이력에 남는다(nullable FK)
    run_id = store.create_run("{}")
    store.record_order(run_id, None, order_no="", symbol="005930", side="buy",
                       order_style="limit", req_price=244_750, req_qty=1,
                       status="rejected",
                       resp_body={"return_msg": "RC4003 호가단위 오류"})
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        row = s.execute(select(TradeOrderRow)).scalar_one()
        assert row.trade_position_id is None and row.status == "rejected"


def test_latest_run_없으면_None(store):
    assert store.latest_run() is None


def test_resp_body_민감키는_영구저장_전_거부(store):
    """§9 최종 방어선(보안 패널 negative-case) — insert-only 감사 행이라 한 번
    새면 삭제 API로도 못 지운다. 헤더 dict·토큰 문자열의 실수 유입 차단."""
    run_id = store.create_run("{}")
    with pytest.raises(ValueError, match="credential-like"):
        store.record_order(run_id, None, order_no="1", symbol="005930",
                           side="buy", order_style="limit", req_price=1_000,
                           req_qty=1, status="submitted",
                           resp_body={"Authorization": "Bearer TOK"})
    with pytest.raises(TypeError, match="response-body dict"):
        store.record_order(run_id, None, order_no="1", symbol="005930",
                           side="buy", order_style="limit", req_price=1_000,
                           req_qty=1, status="submitted",
                           resp_body="raw-token-string")  # type: ignore[arg-type]


def test_오염_행은_행단위_격리되고_나머지는_반환(store, engine):
    """enum 역직렬화 실패 1건이 전체 미종결 목록을 죽이지 않는다(아키텍트 T5 —
    '미종결을 잃지 않는다' 계약). 오염 id는 별도 반환(6c가 warnings 노출)."""
    run_id = store.create_run("{}")
    p1 = store.create_position(run_id, _pos())
    p2 = store.create_position(run_id, _pos(symbol="000660", name="SK하이닉스"))
    from sqlalchemy import text
    with engine.begin() as conn:
        # state는 유효(조회 필터 통과)하되 entry_phase가 손상된 행 — 미지
        # state는 _OPEN_STATES 필터에서 아예 안 나오므로 phase 오염이 실제 경로
        conn.execute(text(
            "UPDATE trade_positions SET entry_phase='garbage' WHERE id=:i"),
            {"i": p1})
    good, corrupted = store.open_positions()
    assert [pid for pid, _ in good] == [p2]
    assert corrupted == [p1]


def test_snapshot_영속은_None을_비움으로_기록(store):
    """save_position_snapshot 계약(P5-T6c 아키텍트 #2) — update_position의
    None=미변경과 달리 스냅샷의 None 필드는 명시적으로 비운다. 스테일
    exit_phase가 남으면 재기동 reconcile이 살아있는 시장가 청산을 오취소."""
    run_id = store.create_run("{}")
    pos_id = store.create_position(run_id, _pos())
    store.update_position(pos_id, state=PositionState.EXITING,
                          exit_phase=ExitPhase.LIMIT_SUBMITTED,
                          exit_reason=ExitReason.TAKE_PROFIT, entered_at=T0)
    # ENTERED 복귀 스냅샷 — phase/reason 명시 clear
    snapshot = _pos(state=PositionState.ENTERED, entry_phase=None)
    store.save_position_snapshot(pos_id, snapshot)
    (pid, pos), = store.open_positions()[0]
    assert pid == pos_id and pos.state is PositionState.ENTERED
    assert pos.exit_phase is None and pos.exit_reason is None
    assert pos.entry_phase is None


def test_주문_상태_전이는_updated_at을_남긴다(store):
    run_id = store.create_run("{}")
    order_id = store.record_order(run_id, None, order_no="1", symbol="005930",
                                  side="buy", order_style="limit",
                                  req_price=1_000, req_qty=1,
                                  status="submitted", resp_body={})
    store.update_order_status(order_id, "cancelled")
    from sqlalchemy import select as sel
    from sqlalchemy.orm import Session
    with Session(store._sessions.kw["bind"]) as s:
        row = s.execute(sel(TradeOrderRow)).scalar_one()
        assert row.updated_at is not None  # 전이 시각(감사 재구성 — 아키텍트 T5)
