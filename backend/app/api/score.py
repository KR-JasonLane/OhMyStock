import asyncio
import json

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.post("/score", status_code=202)
async def start_scoring(request: Request) -> dict:
    if request.app.state.collection.is_running():
        # 수집이 소속/상태/봉을 갱신하는 도중의 반쪽 데이터 읽기 방지 (스펙 §6)
        raise HTTPException(status_code=409,
                            detail="collection is running - retry after it finishes")
    task = request.app.state.scoring.start()
    if task is None:
        raise HTTPException(status_code=409, detail="scoring already running")
    return {"started": True}


@router.get("/score/status")
async def scoring_status(request: Request) -> dict:
    progress = request.app.state.scoring.progress()
    if progress is None:
        return {"status": "idle"}
    body = {"run_id": progress.run_id, "status": progress.status,
            "stage": progress.stage, "done": progress.done,
            "total": progress.total}
    if progress.failure_reason is not None:
        body["failure_reason"] = progress.failure_reason
    return body


@router.get("/score/latest")
async def latest_scores(request: Request) -> dict:
    try:
        results = await asyncio.to_thread(
            request.app.state.scoring_store.latest_results)
    except json.JSONDecodeError:
        # score_runs.config 손상(DB 변조 등) — 내부 정보 노출 없는 일반 오류로 변환
        raise HTTPException(status_code=500,
                            detail="stored scoring config is corrupted")
    if results is None:
        raise HTTPException(status_code=404, detail="no succeeded scoring run")
    return results
