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

Confirmed from the official portal and reference wrappers (re-verify before relying).
Items tagged **"verified live against mock server (2026-07-17)"** were additionally
confirmed by real HTTP calls to `mockapi.kiwoom.com` during Phase 1 (broker adapter)
implementation — see `docs/retrospectives/2026-07-17-phase1-kiwoom-broker-adapter.md`
for the full evidence trail. Items tagged **"verified live against mock server
(2026-07-17, Phase 2)"** were additionally confirmed during Phase 2 (data collection
pipeline) implementation, including a full-universe collection run — see
`docs/retrospectives/2026-07-17-phase2-data-collection-pipeline.md` for the full
evidence trail.

- **Auth:** `POST https://api.kiwoom.com/oauth2/token` with
  `grant_type=client_credentials`, `appkey`, `secretkey`. **Verified live against mock
  server (2026-07-17):** the response contains `token`, `return_code`, and
  `expires_dt` — an **absolute KST timestamp** in `YYYYMMDDHHMMSS` format (not a
  relative TTL in seconds). Reissue logic parses `expires_dt` and reissues with a
  configurable safety margin (default 60s) before it lapses. Token revocation —
  **verified live:** `POST /oauth2/revoke` accepts `{appkey, secretkey, token}` and
  returns `return_code` (0 = success).
- **Mock vs real:** mock `https://mockapi.kiwoom.com` (WS `wss://mockapi.kiwoom.com:10000`),
  real `https://api.kiwoom.com` (WS `wss://api.kiwoom.com:10000`). Toggle via a mock
  flag. Mock is KRX-only. **⚠️ Verified live: mock app key/secret are issued
  SEPARATELY from real-trading key pairs.** Calling the mock endpoint with a
  real-env key pair does not 401 — it returns HTTP 200 with `return_code=2` and
  `return_msg` containing `[8030:투자구분(실전/모의)이 달라서 Appkey를 사용할수가
  없습니다]` ("appkey unusable because real/mock designation differs"). This is a
  credential-provisioning issue, not a client bug — a mock-specific key pair must be
  issued from the Kiwoom developer portal separately from the real-trading pair.
- **TR call pattern — verified live:** every TR call is
  `POST https://{base}/api/dostk/{category}` (categories observed: `stkinfo`
  current-price, `chart` candles, `acnt` account) with headers
  `authorization: Bearer <token>` and `api-id: <TR id>`. Pagination is via response
  headers `cont-yn`/`next-key`, echoed into the next request's `cont-yn`/`next-key`
  headers to continue.
- **Candles:** daily `ka10081`, minute `ka10080`, weekly `ka10082`, monthly `ka10083`,
  tick `ka10079`. Pagination via `cont_yn` + `next_key` (loop for 6 months of history).
  **Verified live: field names match research exactly** — daily-candle rows
  (`stk_dt_pole_chart_qry` array) use `dt`/`open_pric`/`high_pric`/`low_pric`/
  `cur_prc`/`trde_qty`; current-price (`ka10001`) uses `stk_nm`/`cur_prc`/`flu_rt`/
  `trde_qty`. **⚠️ `ka10081` requires a non-empty `base_dt` (`YYYYMMDD`) in
  the request body — an empty string is rejected** with
  `[1511:필수 입력 값이 존재하지 않습니다. 필수입력파라미터=base_dt]`. The adapter
  sends today's date (KST) by default. **Verified live against mock server
  (2026-07-17, Phase 2 PRE-GATE, `.superpowers/sdd/phase2-pregate-basedt.txt`):**
  `base_dt` is the as-of query date — non-business days auto-correct to the prior
  business day (no error), past dates return history as of that date (backfill is
  possible), and future dates clamp to today. **The raw daily
  candle response is descending (newest → oldest)** — callers/adapters must re-sort
  to ascending (oldest → newest) if that ordering is required.
- **Orders:** buy `kt10000`, sell `kt10001`, modify `kt10002`, cancel `kt10003`.
  Order types (호가구분): `00` limit, `03` market, `05` conditional-limit, plus
  IOC/FOK and after-hours variants. Not exercised in Phase 1 (deferred to Phase 5).
- **⚠️ No native TP/SL/Stop or conditional auto-orders via REST.** Stop-loss /
  take-profit must be implemented **client-side**: monitor the execution price
  (WebSocket `0B`) and send a market/limit order when a threshold is hit.
- **⚠️ Rate limit:** ~1 req/s per TR (API ID), burst ~2, **per-TR not global**;
  HTTP 429 on excess. Use a token-bucket limiter + 429 retry. Collecting all ~2,800
  symbols' candles is an overnight batch, not a real-time operation. **Still not
  independently confirmed from an official source** — the Phase 1 rate limiter
  implements these as *configurable defaults* (not hardcoded), so official numbers
  can be applied later without a code change.
- **Account queries — verified live:** `kt00001` (deposit, body `qry_tp=3`)
  top-level fields `entr` (예수금/deposit) and `ord_alow_amt` (주문가능금액/order-
  available amount); `kt00018` (balance, body `qry_tp=1, dmst_stex_tp=KRX`)
  top-level fields `tot_evlt_amt` (총평가금액) and `tot_evlt_pl` (총평가손익). **All
  four fields are present in the response even when the mock account holds zero
  positions** (values are `0`, not absent/omitted) — safe to hard-index rather than
  defensively `.get()`. **Still unverified (pending):** `kt00018`'s row-level fields
  inside the `acnt_evlt_remn_indv_tot` array (`stk_cd`, `stk_nm`, `pur_pric`,
  `cur_prc`, `evlt_amt`, etc.) and whether `avg_price`/`pur_pric` carries fractional
  won — the mock account had zero positions throughout Phase 1, so these were never
  actually returned by the server. **PRE-GATE before Phase 5:** verify against a
  real mock-account position (live test already exists:
  `test_live_잔고_원본응답_avg_price_실측`).
- **Real-time WebSocket:** `0B` execution, `0D` order book, `04` balance, etc. Not
  exercised in Phase 1 (deferred to Phase 5).
- **Catalog TRs — verified live against mock server (2026-07-17, Phase 2):**
  - `ka10099` (`POST /api/dostk/stkinfo`, body `mrkt_tp`: `"0"`=kospi, `"10"`=kosdaq,
    `"8"`=etf) lists instruments; list key `"list"`; row fields include `code`/
    `name`/`marketCode`/`marketName`/`upName`/`upSizeName`/`state`/`auditInfo`/
    `kind`/`listCount`/`regDay`/`lastPrice`/`companyClassName`/`orderWarning`/
    `nxtEnable`. Single page per market (no continuation observed at these row
    counts). **⚠️ The raw response mixes marketCodes regardless of the requested
    market** — `mrkt_tp="0"` (kospi) returns 2,478 raw rows but only **919** have
    `marketCode="0"` (pure kospi common stock); the remainder are ETFs
    (`marketCode="8"`, 1,147 rows) plus 6 other marketCodes (412 rows combined).
    Callers must filter rows by `marketCode`, not trust the request parameter alone
    (measured kospi: 2,478 raw → 919 actual).
  - `ka10101` (same endpoint, body `mrkt_tp`: `"0"`=kospi, `"1"`=kosdaq — **note:
    the kosdaq value differs from ka10099's `"10"`**) lists sector codes; list key
    `"list"`; row fields `marketCode`/`code`/`name`/`group`. Measured **31 kospi +
    34 kosdaq** sectors.
  - `ka20002` (`POST /api/dostk/sect`) lists sector members; **requires all three
    body fields `mrkt_tp` + `inds_cd` + `stex_tp`** (`stex_tp` accepts `"1"` or
    `"KRX"`) — omitting any one is rejected with `[1511:필수 입력 값이 존재하지
    않습니다]`. List key `"inds_stkpc"`; row field `stk_cd`. **Aggregate sector
    `001` (종합(KOSPI)) contains effectively the entire market (2,477 members) —
    aggregate sector codes must be filtered out before using sector membership for
    scoring/rotation** (same pattern expected for kosdaq's `101`).
- **⚠️ Token semantics — verified live (Phase 2): an invalid/superseded token does
  NOT return HTTP 401** — it returns **HTTP 200 with `return_code != 0` and
  `return_msg` containing `[8005:...]`**. The client treats 8005 as an
  invalidate-and-reissue-once trigger, identical in spirit to the 401 path
  (`backend/app/adapters/kiwoom/client.py`, commit `50391ac`). **(Unconfirmed
  inference from measurement, not from official docs): an appkey appears to have
  only one valid token at a time** — issuing a new token from a second process
  invalidated the backend's in-flight token during the Phase 2 full-collection run
  (this is how the 8005 case was discovered). Operational rule: **never issue a
  Kiwoom token from a host-side script/second process while the backend is running**
  a live `TokenManager` against the same appkey.
- **Degenerate candle responses exist:** symbol `012510` returned a single candle
  row with all OHLCV fields as empty strings during the full-universe run.
  Client-side OHLC validation (`Candle.__post_init__`, domain-level, fail-loud)
  correctly rejects this construction; the adapter converts it to `BrokerError` and
  `CollectionService` records it as a per-symbol failure rather than silently
  persisting zero-valued data.
- **Full-universe collection measured (Phase 2, mock server):** 3,887 active
  instruments (post `marketCode`-filter, deduplicated across kospi/kosdaq/etf);
  full daily-candle collection took **~67 minutes** at the ~1 req/s per-TR rate
  limit (22:18–23:25 KST); **2,120,535 candle rows** written; 3,886 succeeded / 1
  failed (`012510` above). Idempotent rerun (resume-skip via
  `CollectionStore.latest_candle_date`) completed in **~2 minutes** with an
  unchanged candle count and the same single failure.

Sources: https://openapi.kiwoom.com/guide/index , https://github.com/younghwan91/kiwoom-rest-api,
live verification against `mockapi.kiwoom.com` (2026-07-17, Phase 1 implementation;
2026-07-17, Phase 2 implementation including full-universe collection).

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
