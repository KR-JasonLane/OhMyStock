# Phase 0 워킹 스켈레톤 구현 계획서

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

- **날짜:** 2026-07-14
- **근거 spec:** `docs/specs/2026-06-16-phase0-walking-skeleton-design.md`

**목표:** 컨테이너 FastAPI 백엔드 ↔ PostgreSQL ↔ 호스트 네이티브 Electron UI를 관통하는
최소 end-to-end 워킹 스켈레톤을 TDD로 구축한다.

**아키텍처:** 백엔드+DB는 docker-compose 컨테이너, Electron UI는 호스트에서 실행되어
`localhost` REST(`/health`) + WebSocket(`/ws`)으로 백엔드에 접속한다. 백엔드는
`api/ core/ adapters/ domain/ store/` 계층 구조를 스텁 포함으로 확립한다.

**기술 스택:** Python 3.12 + uv + FastAPI + uvicorn + SQLAlchemy 2 + Alembic +
psycopg3 + pydantic-settings + pytest / Node 20 + pnpm + electron-vite(React+TS) +
vitest + @testing-library/react / PostgreSQL 16 / docker-compose.

## 전역 제약 (모든 태스크에 암묵 적용)

- Python **3.12**, 패키지 관리는 **uv** (`uv sync`, `uv run pytest`).
- Node **20**, 패키지 관리는 **pnpm**.
- 백엔드 계층: `api/`(transport만), `core/`(설정·로깅), `adapters/`, `domain/`, `store/`.
- 모의투자 우선: 기본 `KIWOOM_MOCK=true`. Phase 0에서는 **키움 API를 절대 호출하지 않는다**
  (환경변수 존재 검증만).
- 모든 문서는 **한국어** (CLAUDE.md만 영어).
- 커밋 메시지는 conventional commits (`feat:`, `test:`, `docs:`, `chore:`).
- `.env`는 절대 커밋 금지 (`.gitignore`에 이미 등록됨). 커밋 대상은 `.env.example`.
- 작업 디렉터리: 백엔드 명령은 `backend/`에서, 프론트 명령은 `frontend/`에서 실행.
- 호스트 선행조건: Docker Desktop, uv, Node 20 + pnpm 설치되어 있음 (없으면 먼저 설치).

## 파일 구조 (최종 산출물)

```
OhMyStock/
├─ docker-compose.yml            # Task 6
├─ .env.example                  # Task 6
├─ backend/                      # Task 1~6
│  ├─ pyproject.toml
│  ├─ uv.lock
│  ├─ Dockerfile
│  ├─ .dockerignore
│  ├─ alembic.ini
│  ├─ alembic/
│  │  ├─ env.py
│  │  └─ versions/0001_create_app_meta.py
│  ├─ app/
│  │  ├─ __init__.py
│  │  ├─ main.py                 # 앱 팩토리 (Task 4)
│  │  ├─ api/{__init__.py, health.py, ws.py}
│  │  ├─ core/{__init__.py, config.py}
│  │  ├─ adapters/__init__.py    # 스텁 (Phase 1에서 BrokerPort)
│  │  ├─ domain/__init__.py      # 스텁
│  │  └─ store/{__init__.py, db.py}
│  └─ tests/{test_config.py, test_db.py, test_migrations.py, test_health.py, test_ws.py}
├─ frontend/                     # Task 7~8 (electron-vite react-ts 템플릿 기반)
│  └─ src/renderer/src/
│     ├─ components/StatusPanel.tsx
│     ├─ hooks/useBackendStatus.ts
│     ├─ App.tsx                 # 수정
│     └─ __tests__/StatusPanel.test.tsx
└─ docs/architecture/system-overview.md   # Task 9
```

---

### Task 1: 백엔드 패키지 뼈대 + 설정 로더 (`core/config.py`)

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/app/__init__.py`, `backend/app/api/__init__.py`,
  `backend/app/core/__init__.py`, `backend/app/adapters/__init__.py`,
  `backend/app/domain/__init__.py`, `backend/app/store/__init__.py` (전부 빈 파일)
- Create: `backend/app/core/config.py`
- Test: `backend/tests/test_config.py`

**Interfaces:**
- Produces: `Settings` (pydantic-settings 모델; 필드 `kiwoom_app_key: str`,
  `kiwoom_secret_key: str`, `kiwoom_mock: bool = True`, `database_url: str`,
  프로퍼티 `mode -> str` = `"mock"|"real"`), `get_settings() -> Settings` (lru_cache).
  이후 모든 태스크가 이 두 이름을 사용한다.

- [ ] **Step 1: pyproject.toml + 패키지 레이아웃 생성**

`backend/pyproject.toml`:

```toml
[project]
name = "ohmystock-backend"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "sqlalchemy>=2.0",
    "alembic>=1.13",
    "psycopg[binary]>=3.2",
    "pydantic-settings>=2.3",
]

[dependency-groups]
dev = [
    "pytest>=8.2",
    "httpx>=0.27",
]

[tool.uv]
package = false

[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
```

빈 `__init__.py` 6개(위 Files 목록)와 `backend/tests/` 디렉터리를 만든 뒤:

```bash
cd backend && uv sync
```

Expected: `.venv` 생성, `uv.lock` 생성, 의존성 설치 성공.

- [ ] **Step 2: 실패하는 테스트 작성**

`backend/tests/test_config.py`:

```python
import pytest
from pydantic import ValidationError

from app.core.config import Settings

ENV = {
    "KIWOOM_APP_KEY": "test-key",
    "KIWOOM_SECRET_KEY": "test-secret",
    "KIWOOM_MOCK": "true",
    "DATABASE_URL": "sqlite+pysqlite:///:memory:",
}


def _set_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


def test_모든_환경변수를_로드한다(monkeypatch):
    _set_env(monkeypatch)
    s = Settings(_env_file=None)
    assert s.kiwoom_app_key == "test-key"
    assert s.kiwoom_secret_key == "test-secret"
    assert s.kiwoom_mock is True
    assert s.database_url == ENV["DATABASE_URL"]
    assert s.mode == "mock"


def test_필수_환경변수_누락시_즉시_실패한다(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.delenv("KIWOOM_APP_KEY")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_mock_false면_mode는_real(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("KIWOOM_MOCK", "false")
    assert Settings(_env_file=None).mode == "real"
```

- [ ] **Step 3: 실패 확인**

Run: `cd backend && uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.config'` 또는 ImportError.

- [ ] **Step 4: 최소 구현**

`backend/app/core/config.py`:

```python
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경변수 기반 설정. 필수값 누락 시 ValidationError로 즉시 실패(fail fast)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    kiwoom_app_key: str
    kiwoom_secret_key: str
    kiwoom_mock: bool = True
    database_url: str

    @property
    def mode(self) -> str:
        return "mock" if self.kiwoom_mock else "real"


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 5: 통과 확인**

Run: `cd backend && uv run pytest tests/test_config.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: 커밋**

```bash
git add backend/pyproject.toml backend/uv.lock backend/app backend/tests
git commit -m "feat(backend): package skeleton + fail-fast settings loader"
```

---

### Task 2: DB 연결 모듈 (`store/db.py`)

**Files:**
- Create: `backend/app/store/db.py`
- Test: `backend/tests/test_db.py`

**Interfaces:**
- Consumes: `Settings` (Task 1).
- Produces: `create_db_engine(settings: Settings) -> Engine`,
  `check_db(engine: Engine) -> bool` (SELECT 1 성공 여부). Task 4/5가 사용.

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/test_db.py`:

```python
from app.core.config import Settings
from app.store.db import check_db, create_db_engine


def _settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        kiwoom_app_key="k",
        kiwoom_secret_key="s",
        kiwoom_mock=True,
        database_url=database_url,
    )


def test_정상_DB면_check_db_True():
    engine = create_db_engine(_settings("sqlite+pysqlite:///:memory:"))
    assert check_db(engine) is True


def test_연결_불가_DB면_check_db_False(tmp_path):
    # 존재하지 않는 하위 디렉터리의 sqlite 파일 → 연결 시 OperationalError
    bad = tmp_path / "no" / "such" / "dir" / "x.db"
    engine = create_db_engine(_settings(f"sqlite+pysqlite:///{bad}"))
    assert check_db(engine) is False
```

- [ ] **Step 2: 실패 확인**

Run: `cd backend && uv run pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.store.db'`.

- [ ] **Step 3: 최소 구현**

`backend/app/store/db.py`:

```python
from sqlalchemy import Engine, create_engine, text

from app.core.config import Settings


def create_db_engine(settings: Settings) -> Engine:
    return create_engine(settings.database_url, pool_pre_ping=True)


def check_db(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
```

- [ ] **Step 4: 통과 확인**

Run: `cd backend && uv run pytest tests/test_db.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: 커밋**

```bash
git add backend/app/store/db.py backend/tests/test_db.py
git commit -m "feat(backend): db engine factory + health check"
```

---

### Task 3: Alembic 마이그레이션 (`app_meta` 테이블)

**Files:**
- Create: `backend/alembic.ini`
- Create: `backend/alembic/env.py`
- Create: `backend/alembic/versions/0001_create_app_meta.py`
- Test: `backend/tests/test_migrations.py`

**Interfaces:**
- Consumes: 환경변수 `DATABASE_URL` (env.py가 직접 읽음 — 컨테이너/테스트 공용 경로).
- Produces: 테이블 `app_meta(key VARCHAR(64) PK, value VARCHAR(255) NOT NULL)`;
  리비전 id `0001`. Task 6의 컨테이너 기동 시 `alembic upgrade head`로 실행됨.

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/test_migrations.py`:

```python
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_마이그레이션이_app_meta_테이블을_만든다(tmp_path, monkeypatch):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'mig.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))

    command.upgrade(cfg, "head")

    insp = inspect(create_engine(db_url))
    assert "app_meta" in insp.get_table_names()
    cols = {c["name"] for c in insp.get_columns("app_meta")}
    assert cols == {"key", "value"}
```

- [ ] **Step 2: 실패 확인**

Run: `cd backend && uv run pytest tests/test_migrations.py -v`
Expected: FAIL — alembic.ini 없음 (`FileNotFoundError` 또는 alembic 설정 오류).

- [ ] **Step 3: Alembic 구성 작성**

`backend/alembic.ini`:

```ini
[alembic]
script_location = alembic

[loggers]
keys = root

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
```

`backend/alembic/env.py` (Phase 0은 online 모드만 지원 — autogenerate를 쓰지 않으므로
`target_metadata = None`):

```python
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def run_migrations_online() -> None:
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
```

`backend/alembic/versions/0001_create_app_meta.py`:

```python
"""create app_meta

Revision ID: 0001
Revises:
Create Date: 2026-07-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_meta",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.String(255), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("app_meta")
```

- [ ] **Step 4: 통과 확인**

Run: `cd backend && uv run pytest tests/test_migrations.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: 커밋**

```bash
git add backend/alembic.ini backend/alembic backend/tests/test_migrations.py
git commit -m "feat(backend): alembic setup + app_meta migration"
```

---

### Task 4: FastAPI 앱 팩토리 + `GET /health`

**Files:**
- Create: `backend/app/main.py`
- Create: `backend/app/api/health.py`
- Test: `backend/tests/test_health.py`

**Interfaces:**
- Consumes: `Settings`, `get_settings` (Task 1); `create_db_engine`, `check_db` (Task 2).
- Produces: `create_app(settings: Settings | None = None) -> FastAPI` —
  `app.state.settings`, `app.state.engine`(lifespan에서 생성) 보유.
  `GET /health` → `{"status": "ok"|"degraded", "db": "ok"|"error", "mode": "mock"|"real"}`.
  Task 5가 같은 앱 팩토리에 WS 라우터를 추가하고, Task 6이
  `uvicorn app.main:create_app --factory`로 기동한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/test_health.py`:

```python
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def _settings(database_url: str = "sqlite+pysqlite:///:memory:") -> Settings:
    return Settings(
        _env_file=None,
        kiwoom_app_key="k",
        kiwoom_secret_key="s",
        kiwoom_mock=True,
        database_url=database_url,
    )


def test_health_정상이면_ok(monkeypatch):
    app = create_app(_settings())
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok", "mode": "mock"}


def test_health_DB_다운이면_degraded(tmp_path):
    bad = tmp_path / "no" / "such" / "dir" / "x.db"
    app = create_app(_settings(f"sqlite+pysqlite:///{bad}"))
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "degraded", "db": "error", "mode": "mock"}
```

- [ ] **Step 2: 실패 확인**

Run: `cd backend && uv run pytest tests/test_health.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.main'`.

- [ ] **Step 3: 최소 구현**

`backend/app/api/health.py`:

```python
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
```

`backend/app/main.py` — 모듈 레벨에서 `create_app()`을 호출하지 않는다
(임포트만으로 환경변수를 요구하면 테스트가 깨짐; uvicorn은 `--factory`로 기동):

```python
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.core.config import Settings, get_settings
from app.store.db import create_db_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = create_db_engine(settings)
        yield
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
    return app
```

- [ ] **Step 4: 통과 확인 (전체 회귀 포함)**

Run: `cd backend && uv run pytest -v`
Expected: PASS (지금까지 총 8 passed).

- [ ] **Step 5: 커밋**

```bash
git add backend/app/main.py backend/app/api/health.py backend/tests/test_health.py
git commit -m "feat(backend): app factory + /health endpoint"
```

---

### Task 5: WebSocket `/ws` 상태 프레임

**Files:**
- Create: `backend/app/api/ws.py`
- Modify: `backend/app/main.py` (라우터 1줄 추가)
- Test: `backend/tests/test_ws.py`

**Interfaces:**
- Consumes: `create_app` (Task 4), `check_db` (Task 2).
- Produces: WS `/ws` — 연결 수락 직후 JSON 프레임 1개 전송:
  `{"backend": "ok", "db": "ok"|"error", "mode": "mock"|"real"}`.
  이후 연결을 유지(클라이언트가 끊을 때까지). 프론트엔드(Task 8)가 이 프레임 형태에 의존.

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/test_ws.py`:

```python
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        kiwoom_app_key="k",
        kiwoom_secret_key="s",
        kiwoom_mock=True,
        database_url="sqlite+pysqlite:///:memory:",
    )


def test_ws_연결시_상태_프레임을_보낸다():
    app = create_app(_settings())
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            frame = ws.receive_json()
    assert frame == {"backend": "ok", "db": "ok", "mode": "mock"}
```

- [ ] **Step 2: 실패 확인**

Run: `cd backend && uv run pytest tests/test_ws.py -v`
Expected: FAIL — 403/404 (라우트 없음, `WebSocketDisconnect`).

- [ ] **Step 3: 최소 구현**

`backend/app/api/ws.py`:

```python
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
```

`backend/app/main.py` 수정 — import 블록에 1줄, `include_router` 1줄 추가:

```python
from app.api.ws import router as ws_router
```

```python
    app.include_router(health_router)
    app.include_router(ws_router)
```

- [ ] **Step 4: 통과 확인**

Run: `cd backend && uv run pytest -v`
Expected: PASS (총 9 passed).

- [ ] **Step 5: 커밋**

```bash
git add backend/app/api/ws.py backend/app/main.py backend/tests/test_ws.py
git commit -m "feat(backend): /ws status frame endpoint"
```

---

### Task 6: Dockerfile + docker-compose + `.env.example`

**Files:**
- Create: `backend/Dockerfile`, `backend/.dockerignore`
- Create: `docker-compose.yml` (레포 루트)
- Create: `.env.example` (레포 루트)

**Interfaces:**
- Consumes: 백엔드 전체 (Task 1~5), 리비전 `0001` (Task 3).
- Produces: `docker compose up` 한 방으로 db(postgres:16, healthcheck) → backend
  (마이그레이션 후 uvicorn, 호스트 포트 **8000**) 기동. 프론트엔드(Task 8)와 수동
  E2E(Task 10)가 `http://127.0.0.1:8000`에 의존.

- [ ] **Step 1: Dockerfile 작성**

`backend/Dockerfile`:

```dockerfile
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

ENV PATH="/app/.venv/bin:$PATH"

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000"]
```

`backend/.dockerignore`:

```
.venv
__pycache__
.pytest_cache
tests
```

- [ ] **Step 2: docker-compose.yml + .env.example 작성**

`docker-compose.yml` (레포 루트):

```yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: ohmystock
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-ohmystock}
      POSTGRES_DB: ohmystock
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ohmystock -d ohmystock"]
      interval: 3s
      timeout: 3s
      retries: 10

  backend:
    build: ./backend
    env_file: .env
    environment:
      DATABASE_URL: postgresql+psycopg://ohmystock:${POSTGRES_PASSWORD:-ohmystock}@db:5432/ohmystock
    ports:
      - "8000:8000"
    depends_on:
      db:
        condition: service_healthy

volumes:
  pgdata:
```

`.env.example` (레포 루트 — 사용자는 이를 `.env`로 복사):

```
# 키움 REST API (Phase 0에서는 존재 검증만 — 더미값이어도 기동됨)
KIWOOM_APP_KEY=your-app-key
KIWOOM_SECRET_KEY=your-secret-key
KIWOOM_MOCK=true

# PostgreSQL
POSTGRES_PASSWORD=ohmystock
```

- [ ] **Step 3: 기동 검증**

```bash
cp .env.example .env
docker compose up --build -d
docker compose ps
```

Expected: `db` (healthy), `backend` (running). 이어서:

```bash
curl -s http://127.0.0.1:8000/health
```

Expected: `{"status":"ok","db":"ok","mode":"mock"}`

마이그레이션 적용 확인:

```bash
docker compose exec db psql -U ohmystock -d ohmystock -c "\dt"
```

Expected: `alembic_version`, `app_meta` 테이블 존재.

실패 시: `docker compose logs backend`로 원인 확인 후 수정 (다음 단계로 넘어가지 말 것).

- [ ] **Step 4: 커밋**

```bash
git add backend/Dockerfile backend/.dockerignore docker-compose.yml .env.example
git commit -m "feat: dockerize backend + compose (db healthcheck, auto-migrate)"
```

---

### Task 7: 프론트엔드 스캐폴드 + `StatusPanel` 컴포넌트

**Files:**
- Create: `frontend/` (electron-vite `react-ts` 템플릿 전체)
- Create: `frontend/vitest.config.ts`
- Create: `frontend/src/renderer/src/components/StatusPanel.tsx`
- Test: `frontend/src/renderer/src/__tests__/StatusPanel.test.tsx`

**Interfaces:**
- Produces: `StatusPanel({ connected, db?, mode? })` — 순수 표시 컴포넌트.
  `connected: boolean`, `db?: 'ok' | 'error'`, `mode?: 'mock' | 'real'`.
  Task 8의 `App.tsx`가 사용.

- [ ] **Step 1: electron-vite 템플릿 스캐폴드**

레포 루트에서:

```bash
pnpm create @quick-start/electron@latest frontend --template react-ts --skip
cd frontend && pnpm install
```

(플래그가 통하지 않으면 대화형 프롬프트에서 `react-ts` 선택, 나머지 기본값.)

스모크 확인: `pnpm dev` → Electron 창이 뜨면 종료.

- [ ] **Step 2: vitest 설치 + 설정**

```bash
cd frontend
pnpm add -D vitest jsdom @testing-library/react @testing-library/jest-dom @vitejs/plugin-react
```

`frontend/vitest.config.ts`:

```ts
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true
  }
})
```

`frontend/package.json`의 `scripts`에 추가:

```json
"test": "vitest run"
```

`frontend/tsconfig.web.json`의 `compilerOptions.types` 배열에 `"vitest/globals"` 추가
(배열이 없으면 생성).

- [ ] **Step 3: 실패하는 테스트 작성**

`frontend/src/renderer/src/__tests__/StatusPanel.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react'
import { StatusPanel } from '../components/StatusPanel'

describe('StatusPanel', () => {
  it('연결 상태를 "Backend: ok · DB: ok · Mode: mock"으로 렌더링한다', () => {
    render(<StatusPanel connected={true} db="ok" mode="mock" />)
    expect(screen.getByRole('status').textContent).toBe('Backend: ok · DB: ok · Mode: mock')
  })

  it('DB 장애 상태를 렌더링한다', () => {
    render(<StatusPanel connected={true} db="error" mode="mock" />)
    expect(screen.getByRole('status').textContent).toBe('Backend: ok · DB: error · Mode: mock')
  })

  it('백엔드 미접속 상태를 렌더링한다 (빈 화면 금지)', () => {
    render(<StatusPanel connected={false} />)
    expect(screen.getByRole('status').textContent).toContain('백엔드 미접속')
  })
})
```

- [ ] **Step 4: 실패 확인**

Run: `cd frontend && pnpm test`
Expected: FAIL — `Cannot find module '../components/StatusPanel'`.

- [ ] **Step 5: 최소 구현**

`frontend/src/renderer/src/components/StatusPanel.tsx`:

```tsx
export interface StatusPanelProps {
  connected: boolean
  db?: 'ok' | 'error'
  mode?: 'mock' | 'real'
}

export function StatusPanel({ connected, db, mode }: StatusPanelProps): React.JSX.Element {
  if (!connected) {
    return <div role="status">백엔드 미접속 — 재연결 시도 중…</div>
  }
  return (
    <div role="status">
      Backend: ok · DB: {db} · Mode: {mode}
    </div>
  )
}
```

- [ ] **Step 6: 통과 확인**

Run: `cd frontend && pnpm test`
Expected: PASS (3 passed).

- [ ] **Step 7: 커밋**

```bash
git add frontend
git commit -m "feat(frontend): electron-vite scaffold + StatusPanel with vitest"
```

---

### Task 8: `useBackendStatus` 훅 + App 통합

**Files:**
- Create: `frontend/src/renderer/src/hooks/useBackendStatus.ts`
- Modify: `frontend/src/renderer/src/App.tsx` (전체 교체)
- Modify: `frontend/src/renderer/index.html` (CSP `connect-src` 허용 추가)

**Interfaces:**
- Consumes: `StatusPanel` (Task 7); 백엔드 `GET /health` + WS `/ws` 프레임 형태
  (Task 4/5: `db`, `mode` 필드).
- Produces: `useBackendStatus(): { connected: boolean; db?: 'ok'|'error'; mode?: 'mock'|'real' }`
  — 시작 시 `/health` 조회 + `/ws` 연결, 끊기면 3초 간격 재연결.

훅의 네트워크 동작은 수동 E2E(Task 10)로 검증한다 — spec §7의 단위 테스트 요구 범위는
상태 컴포넌트 렌더링(Task 7)까지이다.

- [ ] **Step 1: 훅 구현**

`frontend/src/renderer/src/hooks/useBackendStatus.ts`:

```ts
import { useEffect, useState } from 'react'

const BACKEND_HTTP = 'http://127.0.0.1:8000'
const BACKEND_WS = 'ws://127.0.0.1:8000/ws'
const RETRY_MS = 3000

export interface BackendStatus {
  connected: boolean
  db?: 'ok' | 'error'
  mode?: 'mock' | 'real'
}

export function useBackendStatus(): BackendStatus {
  const [status, setStatus] = useState<BackendStatus>({ connected: false })

  useEffect(() => {
    let ws: WebSocket | null = null
    let retryTimer: ReturnType<typeof setTimeout> | undefined
    let disposed = false

    const connect = (): void => {
      fetch(`${BACKEND_HTTP}/health`)
        .then((r) => r.json())
        .then((h) => setStatus({ connected: true, db: h.db, mode: h.mode }))
        .catch(() => setStatus({ connected: false }))

      ws = new WebSocket(BACKEND_WS)
      ws.onmessage = (e): void => {
        const frame = JSON.parse(e.data)
        setStatus({ connected: true, db: frame.db, mode: frame.mode })
      }
      ws.onclose = (): void => {
        if (!disposed) {
          setStatus({ connected: false })
          retryTimer = setTimeout(connect, RETRY_MS)
        }
      }
    }

    connect()
    return (): void => {
      disposed = true
      clearTimeout(retryTimer)
      ws?.close()
    }
  }, [])

  return status
}
```

- [ ] **Step 2: App.tsx 교체**

`frontend/src/renderer/src/App.tsx` (템플릿 데모 내용 전체 삭제 후):

```tsx
import { StatusPanel } from './components/StatusPanel'
import { useBackendStatus } from './hooks/useBackendStatus'

function App(): React.JSX.Element {
  const status = useBackendStatus()
  return (
    <main>
      <h1>OhMyStock</h1>
      <StatusPanel connected={status.connected} db={status.db} mode={status.mode} />
    </main>
  )
}

export default App
```

(템플릿의 `App.tsx`가 참조하던 데모 에셋 import가 남으면 함께 정리 —
`components/Versions` 등 미사용 파일은 삭제.)

- [ ] **Step 3: CSP에 백엔드 허용 추가**

`frontend/src/renderer/index.html`의 `Content-Security-Policy` meta 태그에서
`connect-src`가 백엔드를 허용하도록 수정. 예:

```html
<meta
  http-equiv="Content-Security-Policy"
  content="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self' http://127.0.0.1:8000 ws://127.0.0.1:8000"
/>
```

(템플릿의 기존 CSP 내용을 유지하되 `connect-src` 항목만 추가/확장한다.
이 단계를 빼먹으면 렌더러의 fetch/WS가 조용히 차단된다.)

- [ ] **Step 4: 기존 테스트 회귀 + 수동 확인**

Run: `cd frontend && pnpm test`
Expected: PASS (3 passed).

백엔드 컨테이너가 떠 있는 상태(Task 6)에서:

```bash
cd frontend && pnpm dev
```

Expected: Electron 창에 **"Backend: ok · DB: ok · Mode: mock"** 표시.
`docker compose stop backend` 후 3초 내에 "백엔드 미접속 — 재연결 시도 중…"으로 전환,
`docker compose start backend` 후 자동 복구되면 성공.

- [ ] **Step 5: 커밋**

```bash
git add frontend
git commit -m "feat(frontend): backend status hook + app integration + CSP"
```

---

### Task 9: 마스터 청사진 `docs/architecture/system-overview.md`

**Files:**
- Create: `docs/architecture/system-overview.md`

**Interfaces:**
- Consumes: `CLAUDE.md` §3(아키텍처)·§5(키움 팩트)·§6(로드맵),
  spec §3~4(구성요소·데이터 흐름).
- Produces: 이후 모든 Phase의 spec이 참조할 마스터 청사진 문서 (한국어).

- [ ] **Step 1: 문서 작성**

`docs/architecture/system-overview.md` — 아래 8개 섹션을 모두 포함해 한국어로 작성:

1. **시스템 개요** — OhMyStock 한 문단 요약 (CLAUDE.md §1 내용을 한국어로).
2. **컨테이너 토폴로지** — docker-compose(db, backend, 추후 ollama) + 호스트 Electron
   구도. Phase 0에서 실제 구축된 compose 구성(포트 8000, db healthcheck, 자동
   마이그레이션)을 그대로 기술.
3. **백엔드 계층 구조** — `api/ core/ adapters/ domain/ store/` 각각의 책임과
   "BrokerPort 뒤에 브로커를 숨긴다"는 확장 원칙.
4. **8개 서브시스템** — 로드맵의 Phase 1~8 각각을 2~3문장으로: 브로커 어댑터, 데이터
   수집, 스코어링, AI 멀티에이전트, 트레이딩 엔진(클라이언트측 TP/SL), 스케줄러,
   대시보드, 텔레그램 봇.
5. **데이터 흐름** — Phase 0 현재 흐름(spec §4의 다이어그램) + 완성 시 흐름(수집→스코어
   →AI 필터→주문→모니터링) ASCII 다이어그램.
6. **일일 운영 타임라인** — 장 마감 후 수집(야간 배치, 레이트리밋 TR당 ~1req/s 때문)
   → 자정 스코어링 → 장 전 AI 분석 → 장 중 매매·모니터링.
7. **검증된 키움 REST 팩트 요약** — CLAUDE.md §5의 핵심(모의/실전 URL, 토큰 재발급,
   TR id, 네이티브 TP/SL 부재 → 클라이언트측 구현, 레이트리밋)을 표로. 원본은
   CLAUDE.md §5임을 명시.
8. **로드맵과 의존 관계** — CLAUDE.md §6 표 + Phase 0 완료 상태 반영.

- [ ] **Step 2: 커밋**

```bash
git add docs/architecture/system-overview.md
git commit -m "docs: master architecture blueprint (system-overview)"
```

---

### Task 10: E2E 검증(DoD) + STATUS.md 갱신 + 회고록

**Files:**
- Create: `docs/retrospectives/2026-07-14-phase0-walking-skeleton.md`
- Modify: `docs/STATUS.md`

**Interfaces:**
- Consumes: 전체 산출물 (Task 1~9).
- Produces: Phase 0 완료 선언 + Phase 1 재개 지점.

- [ ] **Step 1: 완료 정의(DoD) 전 항목 검증**

클린 상태에서 순서대로 실행하고 각 항목의 실제 출력을 확인:

```bash
docker compose down && docker compose up --build -d
curl -s http://127.0.0.1:8000/health        # {"status":"ok","db":"ok","mode":"mock"}
cd backend && uv run pytest -v               # 전부 PASS
cd ../frontend && pnpm test                  # 전부 PASS
pnpm dev                                     # Electron 창: "Backend: ok · DB: ok · Mode: mock"
```

하나라도 실패하면 완료 주장 금지 — 원인 수정 후 재검증
(superpowers:verification-before-completion).

- [ ] **Step 2: 회고록 작성**

`docs/retrospectives/2026-07-14-phase0-walking-skeleton.md` — 규칙 4에 따라 비전문가도
따라올 수 있게: 무엇이 요청되었나 / 어떤 구현·변경이 필요했나 / 기존 코드는 어땠나
(이번엔 빈 레포) / 어떤 설계·패턴을 썼나 (앱 팩토리, 계층 구조, fail-fast 설정,
healthcheck 기반 기동 순서, 순수 표시 컴포넌트 + 훅 분리) / 파일·라인 단위 변경 내역.

- [ ] **Step 3: STATUS.md 갱신**

`docs/STATUS.md`의 "▶ 여기서 재개"를 Phase 1(키움 브로커 어댑터 spec 브레인스토밍)로,
워크플로 체크박스에서 Phase 0 항목들을 `[x]`로 갱신. 최종 수정일 갱신.
미해결 선행조건(키움 가입·app key·모의투자 신청)이 이제 **블로커**가 됨을 명시.

- [ ] **Step 4: 커밋**

```bash
git add docs/retrospectives/2026-07-14-phase0-walking-skeleton.md docs/STATUS.md
git commit -m "docs: phase 0 retrospective + status handoff to phase 1"
```
