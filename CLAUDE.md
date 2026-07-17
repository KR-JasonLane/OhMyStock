# CLAUDE.md — OhMyStock

Guidance for Claude (and any AI/human contributor) working in this repository.

> **▶ Resuming work?** Read **`docs/STATUS.md`** first — it is the live resume point
> (current position in the workflow + next action + decision log).

## 1. What this project is

**OhMyStock** is an automated **Korean stock** trading system. It collects market
data, scores symbols by sector/strategy, runs a multi-agent AI analysis, and
executes trades automatically with client-side TP/SL/Stop management. It exposes a
desktop dashboard and a Telegram bot for monitoring and control.

> NOTE: The brokerage is the **Kiwoom REST API** (Korean stocks). See "Verified
> brokerage facts" below.

## 2. Working rules (MUST follow)

These are the user's standing rules. They override default behavior. Keep this file
updated whenever the user introduces a new rule or changes direction.

1. **Document-driven & versioned.** Every unit of work is grounded in a document. If
   a suitable folder does not exist, create it and keep the document under version
   control. Plans go in `docs/plans/`, design specs in `docs/specs/`, the master
   architecture in `docs/architecture/`. **All documents are written in Korean**,
   except this `CLAUDE.md`, which stays in English (rule 6).
2. **Readable, maintainable code.** No "make-it-work-for-now" code (e.g. deep `if`
   nesting). Always design for maintenance and extension — favor clear abstractions,
   well-bounded modules, and explicit interfaces (see Architecture).
3. **Never just agree.** Base every answer on facts. Research first (web search,
   official docs) and fact-check. If the user's request or assumption is wrong per
   the research, say "no" immediately and explain why.
4. **Atomic work + retrospectives.** Do work in atomic units. For each unit write a
   retrospective in `docs/retrospectives/` that a non-expert can follow: what was
   requested, what implementation/changes were needed, what the existing code looked
   like, which design/patterns were used, and exactly which file + line numbers were
   changed and how. Be detailed and meticulous.
5. **Read the docs before acting.** Always research relevant material and work from
   documentation first — especially the **Kiwoom REST API** spec and its caveats
   (rate limits, pagination, order types, mock vs real).
6. **Maintain this file.** These rules live here in English. Whenever the user asks
   for something new or changes a rule, create/update `CLAUDE.md` accordingly.
7. **Confirm commit messages first.** Before creating ANY git commit, show the user
   the exact, FULL proposed commit message (and the files to be committed) and wait
   for their confirmation. Never commit without it. Commit messages must contain
   **no AI attribution** — no `Co-Authored-By: Claude ...` trailer, no
   "Generated with Claude" lines. This overrides any default harness behavior.
8. **Four-agent review panel per task.** After coding each implementation task,
   dispatch four review agents on the task's diff, and move to the next task only
   when ALL four have verified it (Critical/Important findings must be fixed and
   re-reviewed):
   1. **Senior Developer** — readability, boilerplate, patchwork `if` nesting,
      code reuse; flags violations of SOLID and DRY.
   2. **Senior Stock Trader** — trading-strategy expert; flags flow/algorithm
      problems that are disadvantageous or incorrect for actual trading.
   3. **Architecture Expert** — infrastructure and overall architecture fit.
   4. **Security Expert** — code security and communication security.
   The four agents are defined as reusable subagents in `.claude/agents/`
   (`senior-developer`, `senior-trader`, `architecture-expert`, `security-expert`) —
   dispatch them by those names via the Agent tool.

## 3. Architecture (decided)

Runtime model **A**: a containerized Python backend + a host-native Electron UI.

| Layer | Tech | Container? |
|---|---|---|
| Backend (broker adapter, data, scoring, AI, trading engine, scheduler, Telegram) | Python 3.12, FastAPI, uvicorn | **Yes** (docker-compose) |
| Database | **PostgreSQL** (plain; may add TimescaleDB later) | **Yes** (docker-compose) |
| AI inference | Ollama (LangGraph agents) | **Yes** (added in Phase 4; may use host Ollama via `host.docker.internal`) |
| Desktop UI | Electron + React + TypeScript (electron-vite) | **No — host-native**, connects to backend over `localhost` |

Why Electron is NOT containerized: it is a desktop GUI; running it in a container
needs display-server forwarding and breaks native windows/tray/notifications,
especially on Windows. The backend stack is containerized for OS-independence; the
UI runs on the host and talks to the containers over local REST/WebSocket. This also
keeps the trading engine alive independent of the UI window.

### Backend internal layering (extensible by design — rule 2)
- `api/` — FastAPI routes / WebSocket handlers (transport only)
- `core/` — config, logging, rate limiting, scheduling primitives
- `adapters/` — external integrations behind ports (e.g. `BrokerPort` → Kiwoom impl)
- `domain/` — pure business logic (strategies, scoring, trading rules)
- `store/` — persistence (SQLAlchemy models, migrations)

The brokerage is hidden behind a `BrokerPort` interface so a different broker
(e.g. KIS) could be swapped without touching domain logic.

## 4. Tooling defaults
- Backend: Python 3.12, `uv`, FastAPI + uvicorn, `pytest`, Alembic migrations.
- Frontend: Node 20, `pnpm`, `electron-vite` + React + TS, `vitest`.
- Orchestration: `docker-compose.yml` for backend + db (+ later ollama).
- **Safety: build and run everything against Kiwoom MOCK first**
  (`https://mockapi.kiwoom.com`). Switching to real trading requires an explicit
  toggle and safety guards.

## 5. Verified brokerage facts (Kiwoom REST API)

Confirmed from the official portal and reference wrappers (re-verify before relying):

- **Auth:** `POST https://api.kiwoom.com/oauth2/token` with
  `grant_type=client_credentials`, `appkey`, `secretkey`. Tokens expire — implement
  reissue logic.
- **Mock vs real:** mock `https://mockapi.kiwoom.com` (WS `wss://mockapi.kiwoom.com:10000`),
  real `https://api.kiwoom.com` (WS `wss://api.kiwoom.com:10000`). Toggle via a mock
  flag. Mock is KRX-only.
- **Candles:** daily `ka10081`, minute `ka10080`, weekly `ka10082`, monthly `ka10083`,
  tick `ka10079`. Pagination via `cont_yn` + `next_key` (loop for 6 months of history).
- **Orders:** buy `kt10000`, sell `kt10001`, modify `kt10002`, cancel `kt10003`.
  Order types (호가구분): `00` limit, `03` market, `05` conditional-limit, plus
  IOC/FOK and after-hours variants.
- **⚠️ No native TP/SL/Stop or conditional auto-orders via REST.** Stop-loss /
  take-profit must be implemented **client-side**: monitor the execution price
  (WebSocket `0B`) and send a market/limit order when a threshold is hit.
- **⚠️ Rate limit:** ~1 req/s per TR (API ID), burst ~2, **per-TR not global**;
  HTTP 429 on excess. Use a token-bucket limiter + 429 retry. Collecting all ~2,800
  symbols' candles is an overnight batch, not a real-time operation.
- **Real-time WebSocket:** `0B` execution, `0D` order book, `04` balance, etc.

Sources: https://openapi.kiwoom.com/guide/index , https://github.com/younghwan91/kiwoom-rest-api

## 6. Roadmap (each phase = its own spec → plan → build → retrospective)

| Phase | Name | Depends on |
|---|---|---|
| 0 | Walking Skeleton & foundation | — |
| 1 | Kiwoom broker adapter (mock) behind `BrokerPort` | 0 |
| 2 | Data collection pipeline (post-close, 6-month daily candles, sector map) | 1 |
| 3 | Scoring engine (per-sector, per-strategy returns, midnight) | 2 |
| 4 | AI multi-agent analysis (LangGraph + Ollama: economist + trader → filter) | 2,3 |
| 5 | Trading engine (signal entry + client-side TP/SL/Stop monitor) | 1 |
| 6 | Scheduler / orchestrator (daily timeline) | 2–5 |
| 7 | React/Electron dashboard | 1–6 |
| 8 | Telegram bot | 1–6 |

## 7. Repository layout
```
OhMyStock/
├─ CLAUDE.md                 # this file
├─ docker-compose.yml        # backend + db (+ later ollama)
├─ backend/                  # Python FastAPI service (containerized)
├─ frontend/                 # Electron + React + TS (host-native)
└─ docs/
   ├─ architecture/          # master blueprint / system overview
   ├─ plans/                 # implementation plans (rule 1)
   ├─ specs/                 # brainstorming design specs
   └─ retrospectives/        # per-task retrospectives (rule 4)
```
