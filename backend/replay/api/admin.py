"""리플레이 관리 표면(/_replay — 스펙 §9 네임스페이스 격리).

R4 범위: `GET /_replay/status` 최소 구현(§4-2 healthcheck 대상 + §5 speed
스탬프 구조적 강제). R5가 이 파일에 faults/reset 관리 API를 확장한다
(faults.py의 seam 선행 생성과 같은 소유권 패턴 — 플랜 R5 참고).

인증 없음 — 127.0.0.1 바인딩 전제(§4-2 보수: compose는 루프백 포트만
공개, R6). 키움 재현 표면(/api/dostk, /oauth2)과 경로가 절대 겹치지 않는다.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/_replay/status")
async def status(request: Request) -> JSONResponse:
    state = request.app.state
    account = state.account
    return JSONResponse({
        "replay_now": state.clock.now().isoformat(),
        "anchor": state.settings.anchor.isoformat(),
        "speed": state.clock.speed,          # §5 ① — 증거 파일 기록용
        "wall_now": state.wall_now().isoformat(),
        "symbols": len(state.store.symbols),
        "loader_skipped": state.store.skipped,
        "cash": account.cash,
        "reserved_buy": account.reserved_buy_total(),
        "holdings": {s: h.quantity for s, h in account.holdings.items()},
        "open_orders": len(account.open_orders),
        "price_missing_skips": state.engine.price_missing_skips,
        "negative_cash_events": account.negative_cash_events,
        "cost_drift_total": account.cost_drift_total,
    })
