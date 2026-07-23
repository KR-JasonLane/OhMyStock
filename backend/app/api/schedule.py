"""스케줄러 상태/제어 API(P6 스펙 §7). 상태는 무인증(고정 리터럴만 —
스케줄 시각·잡 상태·사유. 포지션·금액·심볼 없음 — §6 reason 계약이 이
전제를 지킨다), pause/resume은 trade 스코프(자동매매 통제 스위치)."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.security import require_trade_token

router = APIRouter()

# recent_events 노출 상한 — 쿼리 파라미터로 받지 않는다(보안 T4 사전 메모:
# 무검증 limit 전달은 응답 팽창 벡터).
_RECENT_EVENTS_LIMIT = 20


def _require_scheduler(request: Request):
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="scheduler is disabled")
    return scheduler


@router.get("/schedule/status")
async def schedule_status(request: Request) -> dict:
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        # 사유는 lifespan 게이트가 판정 시점에 기록한 값을 그대로 조회
        # (아키텍트 T6 — API에서 조건 재계산 금지: 게이트 추가 시 드리프트).
        reason = getattr(request.app.state, "scheduler_disabled_reason",
                         None) or "disabled_by_env"
        return {"enabled": False, "reason": reason}
    snapshot = scheduler.snapshot()
    events = await asyncio.to_thread(
        request.app.state.scheduler_store.recent_events, _RECENT_EVENTS_LIMIT)
    return {"enabled": True, "paused": snapshot["paused"],
            "dead": snapshot["dead"], "jobs": snapshot["jobs"],
            "recent_events": events}


@router.post("/schedule/pause", dependencies=[Depends(require_trade_token)])
async def schedule_pause(request: Request) -> dict:
    scheduler = _require_scheduler(request)
    scheduler.pause()
    return {"paused": True}


@router.post("/schedule/resume", dependencies=[Depends(require_trade_token)])
async def schedule_resume(request: Request) -> dict:
    scheduler = _require_scheduler(request)
    scheduler.resume()
    return {"paused": False}
