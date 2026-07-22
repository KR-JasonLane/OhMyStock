"""리플레이 목 서버 컴포지션 루트(스펙 §4).

기동(팩토리 패턴 — 임포트 시점 부작용 금지: 설정/데이터 적재는 명시 호출):
    uvicorn --factory replay.main:create_app --port 9095
⚠️ **단일 워커 전제**(아키텍트 R4): 계좌/토큰/체결 상태가 전부 프로세스
인메모리라 `--workers N>1`이면 워커별로 계좌가 갈라진다 — R6 compose는
워커 옵션을 명시하지 않는다(uvicorn 기본 1 유지).
필수 env: REPLAY_ANCHOR (KST, 예 2026-07-10T09:00:00). 옵션: REPLAY_SPEED/
REPLAY_DATA_PATH/REPLAY_SYMBOLS(콤마)/REPLAY_PRELOAD_DAYS/REPLAY_CASH/
REPLAY_ETF_SYMBOLS.

조립 계약(플랜 R4):
- MinuteStore는 여기서 1회 적재(부분 적재 — §4)하고 store 참조를 app.state
  에 보관한다(엔진 경유 이중 로드 금지). now_provider 바인딩은
  MatchingEngine.__init__이 수행(구조적 클램프 — R3).
- 모든 응답에 `x-replay-speed` 헤더 스탬프(§5 ① — speed≠1.0 런이 증거
  파일에서 실수로 섞이는 것을 헤더 레벨에서 식별 가능하게).
- 결함 주입은 FaultPolicy 주입 seam뿐(§9) — R5가 관리 API로 확장.
"""

from datetime import datetime

from fastapi import FastAPI

from replay.account import Account
from replay.api import acnt, admin, auth, ordr, stkinfo
from replay.clock import KST, ReplayClock
from replay.config import ReplaySettings
from replay.faults import FaultPolicy
from replay.matching import MatchingEngine
from replay.minute_store import MinuteStore
from replay.tokens import TokenRegistry


def _wall_now_kst() -> datetime:
    return datetime.now(KST)


def create_replay_app(settings: ReplaySettings, *,
                      faults: FaultPolicy | None = None,
                      monotonic=None, wall_now=None) -> FastAPI:
    """테스트 주입 지점: faults(결함 시나리오)/monotonic(재생 시계 전진)/
    wall_now(전파 지연 판정) — 프로덕션 기동은 create_app()이 기본값 조립."""
    wall = wall_now or _wall_now_kst
    clock = ReplayClock(settings.anchor, settings.speed, monotonic=monotonic)
    store = MinuteStore.load(settings.data_path,
                             symbols=list(settings.symbols) or None,
                             since=settings.load_since)
    account = Account(cash=settings.cash)
    faults = faults or FaultPolicy()
    engine = MatchingEngine(account, store, replay_now=clock.now,
                            wall_now=wall, faults=faults,
                            default_market=settings.default_market)

    app = FastAPI(title="OhMyStock Replay Mock", docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.clock = clock
    app.state.store = store
    app.state.account = account
    app.state.engine = engine
    app.state.faults = faults
    app.state.wall_now = wall
    app.state.tokens = TokenRegistry(wall)

    @app.middleware("http")
    async def stamp_speed(request, call_next):
        response = await call_next(request)
        response.headers["x-replay-speed"] = str(settings.speed)
        return response

    for module in (auth, stkinfo, ordr, acnt, admin):
        app.include_router(module.router)
    return app


def create_app() -> FastAPI:
    """uvicorn --factory 진입점 — env 기반 설정."""
    return create_replay_app(ReplaySettings.from_env())
