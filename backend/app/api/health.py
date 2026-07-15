from fastapi import APIRouter, Request

from app.store.db import check_db

router = APIRouter()


@router.get("/health")
def health(request: Request) -> dict:
    db_ok = check_db(request.app.state.engine)
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "error",
        "mode": request.app.state.settings.mode,
    }
