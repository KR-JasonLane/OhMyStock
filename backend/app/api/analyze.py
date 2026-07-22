import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.security import require_write_token

router = APIRouter()


@router.post("/analyze", status_code=202, dependencies=[Depends(require_write_token)])
async def start_analysis(request: Request) -> dict:
    task = request.app.state.analysis.start()
    if task is None:
        raise HTTPException(status_code=409, detail="analysis already running")
    return {"started": True}


@router.get("/analyze/status")
async def analysis_status(request: Request) -> dict:
    service = request.app.state.analysis
    progress = service.progress()
    if progress is None:
        return {"status": "idle"}
    # run_id는 스코어링 런 자체가 없어(NOT NULL FK를 채울 수 없어) run을
    # 아예 만들지 못한 유일한 경우에 None이다(AnalysisProgress 계약,
    # service.py 참고) — 여기서 절대 0 등으로 대체하지 않고 null 그대로
    # 노출해 "런 미생성"과 "런 0번"을 혼동하지 않게 한다.
    # stage="economist"는 LLM 파이프라인 구간(이코노미스트+트레이더 노드)
    # 전체를 가리킨다(스펙 §7 addendum) — 소비자는 stage를 세밀한 진행률이
    # 아니라 거친 단계 표시로만 다뤄야 한다.
    # started_at/finished_at은 None 허용으로 항상 포함한다(failure_reason과
    # 달리 조건부 생략하지 않음) — 소비자가 "며칠 지난 succeeded 런"을
    # "방금 끝난 것"으로 오인하지 않게 신선도를 판별할 수 있어야 한다
    # (T1, P4 트레이더 패널 지적). P5 Task 1에서 progress 필드가 아니라 베이스
    # 서비스 타임스탬프(started_at_iso/finished_at_iso)에서 노출한다(4서비스 대칭).
    body = {"run_id": progress.run_id, "status": progress.status,
            "stage": progress.stage, "done": progress.done,
            "total": progress.total, "started_at": service.started_at_iso(),
            "finished_at": service.finished_at_iso()}
    if progress.failure_reason is not None:
        body["failure_reason"] = progress.failure_reason
    return body


@router.get("/analyze/latest")
async def latest_analysis(request: Request) -> dict:
    # score.py의 /score/latest와 달리 JSONDecodeError 가드가 없다: analysis
    # config는 AnalysisConfig.to_json()으로 내부 생성되는 JSON뿐이고
    # (AnalysisStore.latest_results도 config 칼럼 자체를 읽지 않는다),
    # verdicts/reasons/risk_flags 손상은 store 내부에서 항목 단위로
    # 폴백되므로 API 계층까지 JSONDecodeError가 올라오지 않는다.
    results = await asyncio.to_thread(
        request.app.state.analysis.latest_results)
    if results is None:
        raise HTTPException(status_code=404, detail="no succeeded analysis run")
    return results
