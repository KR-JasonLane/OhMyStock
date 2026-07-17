import logging

from fastapi import APIRouter, HTTPException, Request

from app.core.market_calendar import is_market_hours

logger = logging.getLogger(__name__)

router = APIRouter()

_MARKET_HOURS_WARNING = "market-hours run may store unconfirmed candles"


@router.post("/collect", status_code=202)
async def start_collection(request: Request) -> dict:
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
    progress = request.app.state.collection.progress()
    if progress is None:
        return {"status": "idle"}
    body = {"run_id": progress.run_id, "status": progress.status,
            "stage": progress.stage, "done": progress.done,
            "total": progress.total, "failed": progress.failed}
    if progress.warning is not None:
        body["warning"] = progress.warning
    return body
