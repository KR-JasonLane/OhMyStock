import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.adapters.kiwoom.broker import KiwoomBroker
from app.adapters.kiwoom.client import KiwoomHttpClient
from app.adapters.naver.client import NaverNewsClient
from app.adapters.ollama.client import OllamaClient
from app.api.analyze import router as analyze_router
from app.api.collect import router as collect_router
from app.api.health import router as health_router
from app.api.schedule import router as schedule_router
from app.api.score import router as score_router
from app.api.trade import router as trade_router
from app.api.ws import router as ws_router
from app.core import market_calendar
from app.core.config import Settings, get_settings
from app.core.replay_clock import make_replay_clock
from app.domain.analysis.config import AnalysisConfig
from app.domain.analysis.service import AnalysisService
from app.domain.collection import CollectionService
from app.domain.orchestration.config import ScheduleConfig
from app.domain.orchestration.service import SchedulerService
from app.domain.orchestration.timeline import Job
from app.domain.scoring.service import ScoringService
from app.domain.trading.config import TradingConfig
from app.domain.trading.service import TradingService
from app.store.analysis_store import AnalysisStore
from app.store.collection_store import CollectionStore
from app.store.db import create_db_engine
from app.store.scheduler_store import SchedulerStore
from app.store.scoring_store import ScoringStore
from app.store.trading_store import TradingStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger(__name__)


async def _verify_replay_server(base_url: str) -> datetime:
    """override 기동 게이트(트레이더/아키텍트 R6 — §4-1 확장). 반환: 서버의
    현재 재생 시각(앵커로 사용 — 서버가 재생 시각의 SSOT).

    ① 미도달 → 기동 거부: `.env`에 잊힌 override로 mock 검증인 줄 알고
       로컬 스텁(미기동)을 두들기는 상태를 1줄 WARNING이 아니라 fail-loud로.
    ② speed≠1.0 → 기동 거부: 앱 오프셋 시계는 speed=1.0 전제
       (app.core.replay_clock — §5 ③ speed≠1.0 런은 검증 근거 금지).
    ③ 앵커를 서버에서 취득: 앵커 env 이중화(REPLAY_ANCHOR↔앱 별도 변수)의
       값 드리프트와 서버·앱 기동 시차 드리프트를 동시에 제거."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            response = await http.get(f"{base_url}/_replay/status")
            data = response.json()
    except Exception as exc:
        raise RuntimeError(
            f"KIWOOM_BASE_URL_OVERRIDE({base_url})가 설정됐지만 리플레이 "
            f"서버에 도달할 수 없습니다({type(exc).__name__}) — .env에 잊힌 "
            "override이거나 서버 미기동(--profile replay 선기동 필요)."
        ) from exc
    speed = data.get("speed")
    if speed != 1.0:
        raise RuntimeError(
            f"리플레이 서버 speed={speed} — 앱 오프셋 시계는 speed=1.0 "
            "전제입니다(§5 ③: speed≠1.0 런은 검증 근거 사용 금지). 기동 거부.")
    raw_now = data.get("replay_now")
    if not isinstance(raw_now, str):
        # 형태 드리프트도 RuntimeError로 일관(fail-closed는 동일하되
        # KeyError 스택 대신 원인 명시 — 보안 R6 델타 견고성 노트)
        raise RuntimeError(
            "리플레이 서버 /_replay/status 응답에 replay_now가 없습니다 — "
            "형태 드리프트(서버/앱 버전 불일치?). 기동 거부.")
    return datetime.fromisoformat(raw_now)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.api_write_token is None:
            # 쓰기 엔드포인트(/collect,/score,/analyze)가 인증 없이 열려 있음을
            # 기동 시 1회 경고 — 모의투자 로컬 개발 편의이며, 실전 전환
            # 게이트에서는 api_write_token 설정을 필수로 승격한다.
            logger.warning(
                "api_write_token 미설정 - 쓰기 엔드포인트(/collect,/score,/analyze)가 "
                "인증 없이 열려 있음 (실전 전환 전 필수 설정)")
        app.state.engine = create_db_engine(settings)
        try:
            app.state.broker = KiwoomBroker(KiwoomHttpClient(settings))
            # conflict_check 람다는 app.state를 통해 늦은 바인딩되므로 두 서비스의
            # 생성 순서와 무관하다 (아래에서 scoring이 나중에 만들어져도 안전).
            # 상호 배제는 도메인 계약(scoring 서비스 docstring — API 409는
            # 1차 관문일 뿐). P5부터 트레이딩 포함 3자 배타(아키텍트 P5-T7 #1:
            # Phase 6 스케줄러가 HTTP를 우회해 start()를 불러도 진입 조인이
            # 읽는 candles/instruments가 갱신 중이지 않도록 도메인 레벨 보장).
            def _trading_running() -> bool:
                trading = getattr(app.state, "trading", None)
                return trading is not None and trading.is_running()

            app.state.collection_store = CollectionStore(app.state.engine)
            app.state.collection = CollectionService(
                app.state.broker, app.state.collection_store,
                conflict_check=lambda: (app.state.scoring.is_running()
                                        or _trading_running()))
            app.state.scoring_store = ScoringStore(app.state.engine)
            app.state.scoring = ScoringService(
                app.state.scoring_store,
                conflict_check=lambda: (app.state.collection.is_running()
                                        or _trading_running()))

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
            app.state.analysis_store = AnalysisStore(app.state.engine)
            analysis_store = app.state.analysis_store
            app.state.analysis = AnalysisService(
                analysis_store, app.state.llm, app.state.news,
                config=analysis_cfg)

            # 리플레이 프로필 기동 게이트(§4-1 확장) — 서버 프로브(미도달·
            # speed≠1.0 거부) + 서버 재생 시각을 앵커로 취득(SSOT)
            replay_anchor = None
            trading_now = None
            if settings.kiwoom_base_url_override is not None:
                replay_anchor = await _verify_replay_server(
                    settings.kiwoom_base_url_override)
                trading_now = make_replay_clock(replay_anchor)

            # 트레이딩 엔진(P5 Task 7) — §8-1 버그 봉쇄 한도 4종이 전부
            # 설정된 경우에만 조립(하드 게이트: 상한 없이 실주문 엔진이
            # 켜지지 않는다). 미설정이면 /trade/*는 503.
            # 리플레이 프로필에서는 **store에도 재생 시계 주입**(R7 발견② —
            # store 타임스탬프가 실시계면 쿨다운(recent_closed_symbols) 등
            # 시간 비교가 재생 세계와 혼합되어 오발동한다. run 3 실측 재현).
            # 의도(아키텍트 R7-패치 Minor): TRADE_* 미설정으로 트레이딩이
            # 비활성이어도 override만 있으면 주입 — store 시계는 "프로필"
            # 속성이지 "엔진 활성" 속성이 아니다. 수집/스코어링/분석 3
            # 서비스의 store는 **의도적으로 실시계 유지**(1차 비범위 —
            # §4-1 "4서비스" 경계. 리플레이 중 /collect 호출은 비권장).
            app.state.trading_store = TradingStore(app.state.engine,
                                                   now=trading_now)
            if settings.run_environment == "replay":
                # 교차 오염 fail-fast(트레이더 R6 Critical): 같은 DB에 다른
                # 환경(mock/real)의 미종결 포지션이 있으면 리플레이 reconcile
                # 이 그것을 '브로커 미보유→CLOSED'로 오판해 TP/SL 감시에서
                # 이탈시킨다 — 별도 DATABASE_URL 권장(§4-1), 공유는 기동 거부.
                foreign = await asyncio.to_thread(
                    app.state.trading_store.foreign_open_position_count,
                    "replay")
                if foreign:
                    raise RuntimeError(
                        f"리플레이 프로필이 다른 환경의 미종결 포지션 "
                        f"{foreign}건이 있는 DB에 연결됐습니다 — 실전/모의 "
                        "포지션이 리플레이 reconcile로 감시 이탈(CLOSED 오판)"
                        "될 수 있어 기동을 거부합니다. 리플레이는 별도 "
                        "DATABASE_URL을 사용하세요(§4-1).")
            # 좀비 run 정정(P6 Task 7d — 2026-07-24 실측): 컨테이너가
            # graceful 타임아웃을 넘겨 죽으면 finish_run이 못 돌아 run이
            # 영구 'running'으로 남고 그 run의 warnings(0012)가 소실된다.
            # 기동 시점엔 이 프로세스에 실행 중 run이 없으므로(at-most-one
            # 인프로세스 가드) DB의 running은 전부 stale이다.
            # 실패는 격리한다(fail-open): 좀비 정정은 감사 **위생** 작업이지
            # 자금 안전 게이트가 아니다 — 리플레이 교차 오염 검사(아래,
            # fail-loud)와 성격이 다르다. 스키마 미적용 DB(마이그레이션 선행
            # 전·테스트 부팅)에서 이 위생 작업이 기동을 막으면 안 된다.
            try:
                stale = await asyncio.to_thread(
                    app.state.trading_store.close_stale_runs,
                    settings.run_environment)
                if stale:
                    logger.warning(
                        "closed %d stale trade run(s) left 'running' by an "
                        "unclean shutdown (failure_reason=process_restart) — "
                        "their in-memory warnings were lost", stale)
            except Exception as exc:  # noqa: BLE001
                logger.warning("stale trade run cleanup skipped (%s)",
                               type(exc).__name__)

            trade_limits = (settings.trade_max_single_order_krw,
                            settings.trade_max_daily_orders,
                            settings.trade_max_daily_order_krw,
                            settings.trade_min_avg_trading_value_krw)
            if all(v is not None for v in trade_limits):
                trading_cfg = TradingConfig(
                    max_single_order_krw=settings.trade_max_single_order_krw,
                    max_daily_orders=settings.trade_max_daily_orders,
                    max_daily_order_krw=settings.trade_max_daily_order_krw,
                    min_avg_trading_value_krw=(
                        settings.trade_min_avg_trading_value_krw))
                # 리플레이 프로필(§4-1): 위에서 만든 오프셋 시계(store와
                # 동일 인스턴스 — R7 발견② 시계 단일화)를 서비스에 주입
                # (speed=1.0 고정 전제 — app.core.replay_clock 독스트링).
                if trading_now is not None:
                    logger.warning(
                        "trading clock OVERRIDDEN (replay anchor %s — "
                        "server-sourced)", trading_now().isoformat())
                app.state.trading = TradingService(
                    app.state.broker, app.state.broker,
                    app.state.trading_store, trading_cfg, market_calendar,
                    analysis_store.latest_results,
                    conflict_check=lambda: (
                        app.state.collection.is_running()
                        or app.state.scoring.is_running()),
                    now=trading_now,
                    run_environment=settings.run_environment)
            else:
                app.state.trading = None
                logger.warning(
                    "TRADE_* 한도 미설정 - 트레이딩 엔진 비활성 (§8-1 하드 "
                    "게이트: TRADE_MAX_SINGLE_ORDER_KRW/TRADE_MAX_DAILY_ORDERS/"
                    "TRADE_MAX_DAILY_ORDER_KRW/TRADE_MIN_AVG_TRADING_VALUE_KRW "
                    "4종 전부 설정 필요)")

            # ── P6 스케줄러(결정 #37·#39 — 데일리 타임라인 자동화) ──
            # 기동 게이트(스펙 §5): ① 리플레이 프로필은 무조건 미기동(env보다
            # 우선 — 재생 시계와 실시계 트리거 혼합 방지, 리플레이 런은
            # 수동이 정의상 옳다), ② SCHEDULER_ENABLED=false(영속 off).
            app.state.scheduler = None
            app.state.scheduler_store = None
            # 미기동 사유는 **게이트 판정 시점에 기록**하고 /schedule/status는
            # 조회만 한다(아키텍트 T6 Important — 조건식을 API에서 재계산하면
            # 게이트 추가 시 두 파일이 조용히 어긋난다. 재계산 금지, 기록된
            # 사실 조회). 값은 고정 리터럴만.
            app.state.scheduler_disabled_reason = None
            if settings.run_environment == "replay":
                app.state.scheduler_disabled_reason = "replay_profile"
                logger.info("scheduler disabled: replay profile "
                            "(재생 시계·실시계 트리거 혼합 방지 — 스펙 §5)")
            elif not settings.scheduler_enabled:
                app.state.scheduler_disabled_reason = "disabled_by_env"
                logger.info("scheduler disabled: SCHEDULER_ENABLED=false")
            else:
                app.state.scheduler_store = SchedulerStore(
                    app.state.engine, app.state.collection_store,
                    app.state.scoring_store, analysis_store,
                    app.state.trading_store,
                    run_environment=settings.run_environment)
                app.state.scheduler = SchedulerService(
                    {Job.COLLECT: app.state.collection,
                     Job.SCORE: app.state.scoring,
                     Job.ANALYZE: app.state.analysis,
                     Job.TRADE: app.state.trading},
                    app.state.scheduler_store, ScheduleConfig(),
                    market_calendar)
                app.state.scheduler.start()
                logger.info(
                    "scheduler enabled — daily timeline automation active "
                    "(collect 19:00 → score → analyze 08:20 → trade 09:00, "
                    "decisions #37-#40)")
            try:
                yield
            finally:
                # 셧다운 순서(스펙 §8): **스케줄러 최우선 취소·await 완료**
                # — 정리 중인 서비스에 start()를 재호출하거나 dispose된
                # 엔진에 질의하는 경합 차단. 그 후 4-서비스 정리.
                scheduler = app.state.scheduler
                if scheduler is not None:
                    task = scheduler.current_task()
                    if task is not None and not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                services = [app.state.scoring, app.state.collection,
                            app.state.analysis]
                if app.state.trading is not None:
                    services.append(app.state.trading)
                for service in services:
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
    # 호스트 네이티브 Electron 렌더러(dev 서버 포함)의 localhost 접근만 허용
    # (P3/P4 보안 패널 지적: allow_origins=["*"]는 브라우저發 drive-by 트리거를
    # 이론상 허용 — 사용자 결정 2026-07-18 #24). allow_headers=["*"]는
    # X-API-Key를 포함한 모든 헤더를 허용하며 오리진 제한과 독립적이다.
    # ⚠️ CORS ≠ CSRF 방어: 이 오리진 allowlist는 브라우저의 "응답 읽기"만
    # 차단한다 — 커스텀 헤더 없는 단순 요청(폼 POST 등)은 오리진이
    # allowlist 밖이어도 서버까지 도달하므로, 토큰(API_WRITE_TOKEN) 미설정
    # 상태에서는 CSRF성 쓰기 트리거가 여전히 가능하다. 실질적인 쓰기 실행
    # 차단은 security.py의 X-API-Key 검증이 전담한다(실전 전환 시 Settings
    # validator가 토큰 설정을 필수로 강제).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health_router)
    app.include_router(ws_router)
    app.include_router(collect_router)
    app.include_router(score_router)
    app.include_router(analyze_router)
    app.include_router(trade_router)
    app.include_router(schedule_router)
    return app
