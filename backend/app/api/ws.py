from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from app.store.db import check_db

router = APIRouter()


@router.websocket("/ws")
async def ws_status(websocket: WebSocket) -> None:
    await websocket.accept()
    db_ok = check_db(websocket.app.state.engine)
    await websocket.send_json(
        {
            "backend": "ok",
            "db": "ok" if db_ok else "error",
            "mode": websocket.app.state.settings.mode,
        }
    )
    # Phase 0: 상태 프레임 1회 전송 후 클라이언트가 끊을 때까지 유지 (추후 실시간 피드 토대)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
