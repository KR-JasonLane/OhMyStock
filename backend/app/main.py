import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.adapters.kiwoom.broker import KiwoomBroker
from app.adapters.kiwoom.client import KiwoomHttpClient
from app.adapters.naver.client import NaverNewsClient
from app.adapters.ollama.client import OllamaClient
from app.api.analyze import router as analyze_router
from app.api.collect import router as collect_router
from app.api.health import router as health_router
from app.api.score import router as score_router
from app.api.ws import router as ws_router
from app.core.config import Settings, get_settings
from app.domain.analysis.config import AnalysisConfig
from app.domain.analysis.service import AnalysisService
from app.domain.collection import CollectionService
from app.domain.scoring.service import ScoringService
from app.store.analysis_store import AnalysisStore
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

            # AnalysisConfig 기본값으로 Ollama 클라이언트를 만든다(모델/베이스
            # URL/타임아웃 — cfg.to_json()이 매 런 config 스냅샷으로 저장하는
            # 값과 동일한 출처). 네이버 키는 옵셔널이라 둘 다 있을 때만
            # NaverNewsClient를 만들고, 아니면 news=None으로 넘겨 AnalysisService가
            # 뉴스 조회를 생략하고 경고만 남기게 한다(스펙 §4).
            analysis_cfg = AnalysisConfig()
            app.state.llm = OllamaClient(
                analysis_cfg.ollama_base_url, analysis_cfg.model,
                analysis_cfg.temperature, analysis_cfg.llm_timeout_s)
            if (settings.naver_client_id is not None
                    and settings.naver_client_secret is not None):
                app.state.news = NaverNewsClient(
                    settings.naver_client_id, settings.naver_client_secret)
            else:
                app.state.news = None
            # SSOT — OllamaClient를 만든 것과 동일한 analysis_cfg 인스턴스를
            # config=로 넘긴다. 넘기지 않으면 AnalysisService가 내부에서
            # AnalysisConfig()를 새로 만들어 두 설정이 서로 다른 인스턴스가
            # 되고, 실제 LLM 호출 설정과 DB에 남는 config 스냅샷/감사 기록이
            # 드리프트될 수 있다.
            app.state.analysis = AnalysisService(
                AnalysisStore(app.state.engine), app.state.llm, app.state.news,
                config=analysis_cfg)
            try:
                yield
            finally:
                for service in (app.state.scoring, app.state.collection,
                                app.state.analysis):
                    task = service.current_task()
                    if task is not None and not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                await app.state.broker.aclose()
                await app.state.llm.aclose()
                if app.state.news is not None:
                    await app.state.news.aclose()
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
    app.include_router(analyze_router)
    return app
