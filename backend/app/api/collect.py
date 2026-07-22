import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.exclusion import reject_conflicting_runs
from app.api.security import require_write_token
from app.core.market_calendar import is_market_hours

logger = logging.getLogger(__name__)

router = APIRouter()

_MARKET_HOURS_WARNING = "market-hours run may store unconfirmed candles"


@router.post("/collect", status_code=202, dependencies=[Depends(require_write_token)])
async def start_collection(request: Request) -> dict:
    # 3자 배타(§6/§8-1) — 스코어링 읽기 도중·트레이딩 진입 조인 도중의
    # 반쪽 데이터 방지. 실제 방어선은 도메인 conflict_check(main.py).
    reject_conflicting_runs(request, "scoring", "trading")
    service = request.app.state.collection
    warning = _MARKET_HOURS_WARNING if is_market_hours() else None
    task = service.start(warning=warning)
    if task is None:
        raise HTTPException(status_code=409, detail="collection already running")
    if warning is not None:
        logger.warning(
            "collection triggered during market hours - today's candle may be unconfirmed")
        return {"started": True, "warning": warning}
    return {"started": True}


@router.get("/collect/status")
async def collection_status(request: Request) -> dict:
    service = request.app.state.collection
    progress = service.progress()
    if progress is None:
        return {"status": "idle"}
    # started_at/finished_at은 베이스 서비스 타임스탬프에서 노출(P5 Task 1 —
    # 4서비스 대칭, 이전엔 analysis에만 있던 것을 collect/score까지 확장).
    body = {"run_id": progress.run_id, "status": progress.status,
            "stage": progress.stage, "done": progress.done,
            "total": progress.total, "failed": progress.failed,
            "started_at": service.started_at_iso(),
            "finished_at": service.finished_at_iso()}
    if progress.warning is not None:
        body["warning"] = progress.warning
    return body
