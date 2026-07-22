"""트레이딩 엔진 API(P5 Task 7 — 스펙 §6-7/§8-1).

- POST /trade/start, /trade/stop: require_trade_token(주문 스코프 — 결정 #33).
- GET /trade/status, /trade/positions: 개방(조회 — §8-2 이월 표기: Phase 7
  대시보드 전 재평가).
- 3자 배타(§8-1): 수집/스코어링 실행 중 트레이딩 시작 거부(양방향 가드의
  이쪽 절반 — 반대쪽은 collect.py/score.py가 트레이딩 실행을 거부)."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.exclusion import reject_conflicting_runs
from app.api.security import require_trade_token
from app.core.background_service import StopMode

logger = logging.getLogger(__name__)

router = APIRouter()

_STOP_MODES = {mode.value: mode for mode in StopMode}


def _trading(request: Request):
    """§8-1 하드 게이트 — TRADE_* 한도 미설정이면 엔진 자체가 조립되지 않는다.
    getattr — exclusion 헬퍼와 동일한 방어 스타일(아키텍트 Minor 일관성)."""
    service = getattr(request.app.state, "trading", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="trading engine disabled - set TRADE_* limit settings "
                   "(§8-1 hard gate)")
    return service


@router.post("/trade/start", status_code=202,
             dependencies=[Depends(require_trade_token)])
async def start_trading(request: Request) -> dict:
    trading = _trading(request)
    # 3자 배타(§8-1) — 수집/스코어링이 candles/instruments를 갱신하는 도중
    # 진입 조인이 반쪽 데이터를 읽는 것 방지. 공용 헬퍼(개발자 P5-T7 #2),
    # 실제 방어선은 도메인 conflict_check(main.py).
    reject_conflicting_runs(request, "collection", "scoring")
    task = trading.start()
    if task is None:
        raise HTTPException(status_code=409, detail="trading already running")
    logger.info("trading run started")
    return {"started": True}


@router.post("/trade/stop", dependencies=[Depends(require_trade_token)])
async def stop_trading(request: Request, body: dict | None = None) -> dict:
    """협조적 정지(§6-5) — mode: stop_new_entries(기본) | liquidate_all.
    실제 반영은 다음 사이클 경계(원자 구간 보호 — 즉시 취소 아님)."""
    service = _trading(request)
    if not service.is_running():
        raise HTTPException(status_code=409, detail="trading is not running")
    raw = (body or {}).get("mode", StopMode.STOP_NEW_ENTRIES.value)
    mode = _STOP_MODES.get(raw)
    if mode is None:
        raise HTTPException(
            status_code=422,
            detail=f"unknown stop mode {raw!r} — one of {sorted(_STOP_MODES)}")
    service.request_stop(mode)
    logger.warning("trading stop requested: mode=%s", mode.value)
    return {"stopping": True, "mode": mode.value}


@router.get("/trade/status")
async def trading_status(request: Request) -> dict:
    if request.app.state.trading is None:
        return {"status": "disabled",
                "detail": "TRADE_* limit settings not configured"}
    service = request.app.state.trading
    progress = service.progress()
    return {
        "run_id": progress.run_id,
        "status": progress.status,
        "started_at": service.started_at_iso(),
        "finished_at": service.finished_at_iso(),
        "positions_count": progress.positions_count,
        "warnings": list(progress.warnings),
        "daily_order_count": progress.daily_order_count,
        "daily_order_krw": progress.daily_order_krw,
        "kill_switch": progress.kill_switch,
    }


@router.get("/trade/positions")
async def trading_positions(request: Request) -> dict:
    """미종결 포지션 목록(§6-7) — 감사/대시보드용 읽기 전용."""
    store = request.app.state.trading_store
    rows, corrupted = store.open_positions()
    return {
        "positions": [{
            "symbol": pos.symbol, "name": pos.name, "market": pos.market,
            "state": pos.state.value,
            "entry_phase": pos.entry_phase.value if pos.entry_phase else None,
            "exit_phase": pos.exit_phase.value if pos.exit_phase else None,
            "entry_price": pos.entry_price, "quantity": pos.quantity,
            "peak_price": pos.peak_price,
            "trailing_active": pos.trailing_active,
            "exit_reason": pos.exit_reason.value if pos.exit_reason else None,
            "realized_pnl": pos.realized_pnl,
            "entered_at": pos.entered_at.isoformat() if pos.entered_at else None,
        } for _pid, pos in rows],
        "corrupted_rows": corrupted,
    }
