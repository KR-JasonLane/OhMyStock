import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.adapters.kiwoom.broker import KiwoomBroker
from app.adapters.kiwoom.client import KiwoomHttpClient
from app.api.collect import router as collect_router
from app.api.health import router as health_router
from app.api.score import router as score_router
from app.api.ws import router as ws_router
from app.core.config import Settings, get_settings
from app.domain.collection import CollectionService
from app.domain.scoring.service import ScoringService
from app.store.collection_store import CollectionStore
from app.store.db import create_db_engine
from app.store.scoring_store import ScoringStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = create_db_engine(settings)
        try:
            app.state.broker = KiwoomBroker(KiwoomHttpClient(settings))
            # conflict_check 람다는 app.state를 통해 늦은 바인딩되므로 두 서비스의
            # 생성 순서와 무관하다 (아래에서 scoring이 나중에 만들어져도 안전).
            app.state.collection = CollectionService(
                app.state.broker, CollectionStore(app.state.engine),
                conflict_check=lambda: app.state.scoring.is_running())
            app.state.scoring_store = ScoringStore(app.state.engine)
            app.state.scoring = ScoringService(
                app.state.scoring_store,
                conflict_check=lambda: app.state.collection.is_running())
            try:
                yield
            finally:
                for service in (app.state.scoring, app.state.collection):
                    task = service.current_task()
                    if task is not None and not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                await app.state.broker.aclose()
        finally:
            app.state.engine.dispose()

    app = FastAPI(title="OhMyStock Backend", lifespan=lifespan)
    app.state.settings = settings
    # 호스트 네이티브 Electron 렌더러(dev 서버 포함)의 localhost 접근 허용
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health_router)
    app.include_router(ws_router)
    app.include_router(collect_router)
    app.include_router(score_router)
    return app
