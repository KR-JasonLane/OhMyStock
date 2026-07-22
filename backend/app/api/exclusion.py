"""상호 배타 409 가드 공용 헬퍼(개발자 P5-T7 #2 — 3개 라우터에 동일 로직이
스타일만 다르게 복제되던 것의 단일 출처).

API 레벨 409는 1차 관문(UX용 메시지)이고, 실제 방어선은 각 서비스의
conflict_check(도메인 계약 — main.py 배선)다. Phase 6 스케줄러가 네 번째
배타 쌍을 추가할 때도 이 헬퍼를 재사용한다."""

from fastapi import HTTPException, Request


def reject_conflicting_runs(request: Request, *service_names: str) -> None:
    """지정한 app.state 서비스 중 실행 중인 것이 있으면 409.
    미조립 서비스(예: TRADE_* 미설정 → trading=None)는 통과."""
    for name in service_names:
        service = getattr(request.app.state, name, None)
        if service is not None and service.is_running():
            raise HTTPException(
                status_code=409,
                detail=f"{name} is running - retry after it finishes")
