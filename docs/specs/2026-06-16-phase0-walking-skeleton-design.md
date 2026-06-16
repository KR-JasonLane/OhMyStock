# Design Spec — Phase 0: Walking Skeleton & Foundation

- **Date:** 2026-06-16
- **Status:** Draft (awaiting user review)
- **Author:** Claude (brainstorming session)
- **Scope:** First sub-project of OhMyStock. Establishes the repo/doc structure,
  governance (CLAUDE.md), and a minimal end-to-end "walking skeleton" that exercises
  the whole architecture (host-native Electron UI ↔ containerized FastAPI backend ↔
  PostgreSQL) before any feature work begins.

---

## 1. Background & decisions leading here

Captured during brainstorming (see conversation):

1. **Asset class:** Korean **stocks** (not crypto). The original "Upbit" idea was
   rejected — Upbit is a crypto exchange and cannot trade stocks (rule 3 fact-check).
2. **Brokerage:** **Kiwoom REST API** (new REST/WebSocket product), chosen for
   cross-platform compatibility with Electron. The legacy Kiwoom OpenAPI+ (OCX/COM)
   is Windows-only and incompatible with the OS-independence goal.
3. **Runtime architecture (A):** containerized Python (FastAPI) backend + host-native
   Electron/React UI. The trading engine therefore survives the UI being closed.
4. **Containerization boundary:** backend + DB (+ later Ollama) run in
   `docker-compose`; **Electron UI runs on the host** and connects over `localhost`.
   Electron is a desktop GUI and is not suitable for containers.
5. **Database:** **PostgreSQL** (plain). TimescaleDB can be added later if time-series
   query performance requires it.
6. **Safety:** everything is built and run against **Kiwoom MOCK** first.

## 2. Goal of Phase 0

Deliver a **walking skeleton**: a minimal but real end-to-end thread through every
architectural layer, plus the documentation/version-control system. This de-risks the
architecture and gives every later phase a working foundation to build on.

Explicitly **out of scope** for Phase 0: any real Kiwoom API calls, data collection,
scoring, AI, trading logic, or production UI. Those are Phases 1–8.

## 3. Components & boundaries

### 3.1 `backend/` — FastAPI service (containerized)
- `GET /health` → returns service status (`ok`), DB connectivity, and current mode
  (`mock`/`real`).
- WebSocket endpoint (e.g. `/ws`) → on connect, pushes a hello/status frame
  (`{"backend":"ok","db":"ok","mode":"mock"}`); foundation for later real-time feeds.
- **Config loader** (`core/config`): reads env (`.env`) — `KIWOOM_APP_KEY`,
  `KIWOOM_SECRET_KEY`, `KIWOOM_MOCK=true`, DB DSN. Validates presence; does NOT call
  Kiwoom yet.
- **DB connectivity** (`store/`): SQLAlchemy engine + one trivial migration creating a
  `schema_version` (or `app_meta`) table, proving migrations work.
- **Layered structure stubbed** per CLAUDE.md §3 (`api/`, `core/`, `adapters/`,
  `domain/`, `store/`) so later phases drop into clear homes.

### 3.2 `frontend/` — Electron + React + TS (host-native)
- Electron app launches a React window.
- On startup, calls backend `GET /health` and opens the `/ws` WebSocket.
- Renders a single status screen: **"Backend: ok · DB: ok · Mode: mock"**, plus an
  error state if the backend is unreachable.

### 3.3 `db` — PostgreSQL (containerized)
- Plain `postgres` image with a named volume for persistence.
- Credentials/DSN via env, shared with the backend service.

### 3.4 Orchestration — `docker-compose.yml`
- Services: `db` (postgres), `backend` (fastapi). Ollama is added in Phase 4.
- `docker compose up` builds/starts backend + db; backend waits for db health, runs
  migrations, then serves.
- Frontend is launched separately on the host (`pnpm dev` / `pnpm start`).

## 4. Data flow (Phase 0)
```
[Electron/React UI]  --HTTP /health-->  [FastAPI backend]  --SQLAlchemy-->  [Postgres]
        (host)        --WS /ws------->     (container)                       (container)
                       <--status frame--
```

## 5. Documentation & version control (rules 1, 4, 6)
- `CLAUDE.md` (root) — rules in English + architecture + verified Kiwoom facts. (Created.)
- `docs/architecture/system-overview.md` — master blueprint (created during Phase 0
  implementation): 8 subsystems, data flow, container topology, daily timeline,
  verified Kiwoom facts, roadmap.
- `docs/plans/` — the Phase 0 implementation plan (next step, via writing-plans).
- `docs/specs/` — this document.
- `docs/retrospectives/` — a Phase 0 retrospective written on completion (rule 4).
- Git initialized; `.gitignore` for Python/Node/Electron/Docker artifacts.

## 6. Error handling
- Backend: structured logging; `/health` reports degraded state if DB is down;
  config loader fails fast with a clear message if required env is missing.
- Frontend: explicit "backend unreachable" UI state with retry; never a blank screen.
- Compose: backend depends on db healthcheck so it does not race the database.

## 7. Testing
- Backend: `pytest` — config loader (missing/typed env), `/health` response shape,
  migration applies cleanly against a test DB.
- Frontend: `vitest` — status component renders connected vs error states.
- Manual end-to-end (Definition of Done): `docker compose up` → launch Electron →
  UI shows **"Backend: ok · DB: ok · Mode: mock"**. Reproducible from a clean clone.

## 8. Definition of Done
1. `docker compose up` brings up `db` + `backend`; `/health` returns ok with DB
   connected and `mode=mock`.
2. Host Electron app launches and displays the connected status from the backend.
3. `CLAUDE.md`, `docs/architecture/system-overview.md`, and the folder structure exist
   and are committed to git.
4. Backend and frontend unit tests pass.
5. A Phase 0 retrospective exists in `docs/retrospectives/`.

## 9. Risks / open items
- **Kiwoom credentials & mock account:** the user must register at
  `openapi.kiwoom.com`, obtain app key/secret, and apply for mock trading. Needed for
  Phase 1, not Phase 0 (Phase 0 only loads config, no live calls).
- **Ollama GPU on Windows:** containerized Ollama needs NVIDIA + WSL2 GPU passthrough;
  alternative is host-native Ollama via `host.docker.internal`. Decided in Phase 4.
- **Rate limits / TP-SL client-side:** not exercised in Phase 0 but recorded in
  CLAUDE.md so Phases 1/5 design around them.
