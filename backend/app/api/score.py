import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.exclusion import reject_conflicting_runs
from app.api.security import require_write_token

router = APIRouter()


@router.post("/score", status_code=202, dependencies=[Depends(require_write_token)])
async def start_scoring(request: Request) -> dict:
    # 3자 배타(§6/§8-1) — 공용 헬퍼. 실제 방어선은 도메인 conflict_check.
    reject_conflicting_runs(request, "collection", "trading")
    task = request.app.state.scoring.start()
    if task is None:
        raise HTTPException(status_code=409, detail="scoring already running")
    return {"started": True}


@router.get("/score/status")
async def scoring_status(request: Request) -> dict:
    service = request.app.state.scoring
    progress = service.progress()
    if progress is None:
        return {"status": "idle"}
    # started_at/finished_at은 베이스 서비스 타임스탬프에서 노출(P5 Task 1 대칭).
    body = {"run_id": progress.run_id, "status": progress.status,
            "stage": progress.stage, "done": progress.done,
            "total": progress.total, "started_at": service.started_at_iso(),
            "finished_at": service.finished_at_iso()}
    if progress.failure_reason is not None:
        body["failure_reason"] = progress.failure_reason
    return body


@router.get("/score/latest")
async def latest_scores(request: Request) -> dict:
    try:
        results = await asyncio.to_thread(
            request.app.state.scoring.latest_results)
    except json.JSONDecodeError as exc:
        # score_runs.config 손상(DB 변조 등) — 내부 정보 노출 없는 일반 오류로 변환
        raise HTTPException(status_code=500,
                            detail="stored scoring config is corrupted") from exc
    if results is None:
        raise HTTPException(status_code=404, detail="no succeeded scoring run")
    return results
