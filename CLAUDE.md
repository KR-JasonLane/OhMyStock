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

   **8-b. Optional broker-API reviewer (`broker-api-expert`).** For tasks that touch
   the broker API — adapter implementation, TR call sites, PRE-GATE probe scripts —
   additionally dispatch `broker-api-expert` (defined in `.claude/agents/`). It
   verifies our code calls the Kiwoom REST API per spec/measured reality: request
   body/headers/order-type codes, `cont-yn`/`next-key` pagination, response field
   parsing, token/rate-limit handling, mock/real boundary. Its governing rule is
   **measured evidence over documentation** (Kiwoom docs are unreliable — see §5).
   Not part of the always-on 4-agent panel; summon it only when the task is
   API-shaped. Findings at Critical/Important must be fixed and re-reviewed, same as
   rule 8.

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
- **Minute candles `ka10080` — verified live against mock server (2026-07-22,
  replay R1, `.superpowers/sdd/replay-ka10080-probe.txt`):** `POST
  /api/dostk/chart`, body `{stk_cd, tic_scope:"1"(분 단위), upd_stkpc_tp:"1",
  base_dt(YYYYMMDD)}` — **base_dt 포함 바디가 1차 시도에 수락**(랩퍼
  changelog의 "기준일자 추가" 정황과 부합; base_dt 없는 변형은 미측정 —
  포함이 안전). List key `stk_min_pole_chart_qry`; row fields `cntr_tm`
  (**YYYYMMDDHHMMSS**), `open_pric`/`high_pric`/`low_pric`/`cur_prc`
  (**등락 부호 ±prefix 포함** — 파서는 abs 처리 필수; 같은 행 안에서
  필드별 부호가 다를 수 있음 — 등락 방향 표기이지 델타가 아님), `trde_qty`,
  `acc_trde_qty`, `pred_pre`, `pred_pre_sig`. **내림차순(newest→oldest)**,
  900 rows/page, cont-yn/next-key 페이지네이션. **보관 ~13개월**(2026-07-22
  실행 시 2025-07-01 09:00까지). 429는 로그상 3회만 관측(RateLimiter
  백오프로 즉시 흡수 — stdout/stderr 버퍼링으로 정확한 발생 시점 추적
  불가). `012510`은 분봉도 전 필드 빈 문자열 1행(degenerate — 일봉 전례와
  동일 클래스; 이로 인해 "저유동 커버리지" 심볼이 실질 부재 — 대체 심볼
  확보 전까지 저유동 검증 공백). **무거래 분은 봉이 없다**(결측 —
  `replay-ka10080-coverage.txt` 갭 실측: 예 371460 5거래일 중 132분 결측)
  — 리플레이의 "직전가 유지" 정책의 실측 근거. ⚠️ 이 수집은 **장중
  (15:09~15:29 KST) 실행분**이라 당일(7/22) tail 1~2분봉은 미확정 가능 —
  리플레이 앵커는 당일 tail 구간 회피 권장. 전 종목(12) 수집 ~20분.
- **Multi-symbol quote `ka10095` (관심종목정보요청) — verified live against mock
  server (2026-07-22, Phase 5 PRE-GATE G1, `.superpowers/sdd/p5-pregate-G1.txt`):**
  `POST /api/dostk/stkinfo`, body `{stk_cd: "<code1>|<code2>|..."}`. **⚠️ The
  multi-symbol delimiter is a PIPE (`|`), NOT semicolon** — semicolon/comma/space all
  return an empty 1-row response (web docs claiming ";" are WRONG; measured). Codes are
  **plain (no `KRX:` prefix — prefix makes it fail)**. **Max 100 symbols per call**
  (50→50 rows, 100→100 rows, **101→`rc=5` error**); no pagination (cont-yn="N", hard
  cap at 100). List key `atn_stk_infr`; each row has 63 fields including `cur_prc`
  **plus 5-level order book `sel_1th_bid`..`sel_5th_bid` / `buy_1th_bid`..`buy_5th_bid`
  and best `sel_bid`/`buy_bid`** — so a SEPARATE order-book TR is NOT needed for the
  monitor. Partial failure: a bogus code is returned as an empty row (3 requested →
  3 rows, bogus one blank) — filter empty `stk_cd` rows. Pre-open (before 09:00),
  low-liquidity symbols return blank values but liquid symbols (e.g. 005930) return
  base price; intraday all fields populate. This confirms decision #27 (REST polling
  monitor: 1 pipe-call covers ≤5 held symbols in ~1s).
- **Orders — verified live against mock server (2026-07-22, Phase 5 PRE-GATE G2/G3,
  `.superpowers/sdd/p5-pregate-G2.txt`/`p5-pregate-G3.txt`):** buy `kt10000`, sell
  `kt10001`, cancel `kt10003` — all `POST /api/dostk/ordr` (category **`ordr`**,
  not `stkinfo`). **⚠️ Order-type field is `trde_tp` with SINGLE-digit codes, not
  two-digit** — verified live: `"0"`=limit(지정가), `"3"`=market(시장가) both accepted
  (`rc=0`). The earlier "`00`/`03`" note was WRONG (single-digit is correct).
  - **Buy/sell body (identical schema):** `{dmst_stex_tp:"KRX", stk_cd:<맨코드>,
    ord_qty:<str>, trde_tp:<"0"|"3">, ord_uv:<price str, LIMIT ONLY — omit for market>}`.
    **No account-number/password field** (account bound to appkey — same as kt00001/
    kt00018). Response: `{ord_no, dmst_stex_tp, return_code, return_msg}`, msg e.g.
    "모의투자 매수주문완료".
  - **Cancel `kt10003` body:** `{dmst_stex_tp:"KRX", orig_ord_no:<str>, stk_cd,
    cncl_qty:"0"}` — **verified: `orig_ord_no`/`cncl_qty` are the correct field names**
    (community wrapper's `org_ord_no`/`ord_qty` was wrong); `cncl_qty="0"`=전량취소.
    Response: `{ord_no, base_orig_ord_no, cncl_qty, ...}`.
  - **Unfilled-order query `ka10075`** (`POST /api/dostk/acnt`, category `acnt`):
    body `{all_stk_tp:"0", trde_tp:"0", stex_tp:"0"}`; list key **`oso`**; row fields
    include `ord_no`/`stk_cd`/`ord_qty`/`oso_qty`/`ord_pric`/`ord_stt`(="접수")/
    `orig_ord_no`/`stex_tp`/`tm`/`cntr_qty`. `stex_tp="0"` accepted (not "KRX" literal).
  - **SELL+LIMIT verified live (2026-07-22, tests/live/test_live_orders.py):**
    kt10001 with trde_tp="0"+ord_uv accepted (rc=0 "모의투자 매도주문완료") —
    closes the one order-type combination G2/G3 did not cover. **ka10075
    `io_tp_nm` raw value measured: `'-매도'`** (prefixed SUBSTRING, not exact
    "매도") — parsers MUST use containment matching, never equality.
  - **⚠️ Order and quote TRs use SEPARATE rate-limit buckets — verified:** a quote
    (`ka10095`) call immediately after an order (`kt10000`) returned `rc=0` with no 429.
    This RESOLVES the §11-5 risk (손절 order delayed by quote polling) — no order-
    priority design needed (decision #14 concern retired by measurement).
  - **Tick size (호가단위) — discriminating probe verified (2026-07-22,
    `.superpowers/sdd/p5-pregate-tick-probe.txt`):** the 200,000–500,000 KRW band is
    **500 KRW** (matches official 2023 KRX reform docs). A limit order priced at a
    multiple of 250 but not 500 (244,750) was REJECTED with `rc=20` /
    `[2000](RC4003:모의투자 호가단위 오류입니다.)`; a control order (…510) was also
    rejected — so the mock DOES validate tick alignment on limit orders (`RC4003` is
    the tick-violation signal). **⚠️ Mock MARKET-order fill prices do NOT respect the
    tick grid** (G3 fill 272,750 is not a 500-multiple — matching-engine blended/
    approximated price). Never infer the tick table from fill prices; only from
    limit-order accept/reject. Full table in `domain/trading/ticks.py`; other bands
    and ETF (5 KRW, doc-confirmed only) are doc-based — low-priority probe candidates.
- **⚠️ No native TP/SL/Stop or conditional auto-orders via REST.** Stop-loss /
  take-profit must be implemented **client-side**: monitor the execution price
  (WebSocket `0B`) and send a market/limit order when a threshold is hit.
- **⚠️ Rate limit:** ~1 req/s per TR (API ID), burst ~2, **per-TR not global**;
  HTTP 429 on excess. Use a token-bucket limiter + 429 retry. Collecting all
  ~3,900 symbols (measured 3,887 active) candles is an overnight batch, not a
  real-time operation. **Still not
  independently confirmed from an official source** — the Phase 1 rate limiter
  implements these as *configurable defaults* (not hardcoded), so official numbers
  can be applied later without a code change.
- **Account queries — verified live:** `kt00001` (deposit, body `qry_tp=3`)
  top-level fields `entr` (예수금/deposit) and `ord_alow_amt` (주문가능금액/order-
  available amount); `kt00018` (balance, body `qry_tp=1, dmst_stex_tp=KRX`)
  top-level fields `tot_evlt_amt` (총평가금액) and `tot_evlt_pl` (총평가손익). **All
  four fields are present in the response even when the mock account holds zero
  positions** (values are `0`, not absent/omitted) — safe to hard-index rather than
  defensively `.get()`. **✅ Row-level fields — verified live against a real mock
  position (2026-07-22, Phase 5 PRE-GATE G3, `.superpowers/sdd/p5-pregate-G3.txt`):**
  list key `acnt_evlt_remn_indv_tot`; each row has **23 fields**: `stk_cd`
  (**⚠️ `"A005930"` — carries `A` prefix**, `_normalize_symbol` strips it), `stk_nm`,
  `pur_pric` (매입가), `cur_prc`, `evlt_amt` (평가금액), `evltv_prft`, `prft_rt`,
  `rmnd_qty` (보유수량), `trde_able_qty`, `pur_amt`, `pur_cmsn`/`sell_cmsn`/`tax`/
  `sum_cmsn` (매수·매도 수수료/세금/합계), `poss_rt`, `crd_tp`, `tdy_buyq`/`tdy_sellq`,
  `pred_buyq`/`pred_sellq`/`pred_close_pric`. All values are **zero-padded integer
  strings** (e.g. `pur_pric="000000000272750"`). **`pur_pric`/`avg_price` is an
  INTEGER (원 단위, no fractional won)** — the `TradePosition.avg_price: int`
  assumption is confirmed safe. `broker.get_balance()` parsing was validated against
  this real position (positions count matched raw rows; avg_price=272750,
  cur=272500, eval=272500). **Cost fields measured** (삼성전자 272,750원 1주):
  `tax=544` ≈ 0.2% of proceeds (코스피 매도세+농특세, matches), `pur_cmsn`/`sell_cmsn`
  each 950 (mock commission ~0.35%, HIGHER than real ~0.015% — keep `costs.py` rates
  configurable, adjust at live cutover). `evltv_prft`/`prft_rt` can be negative.
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
- **⚠️ Mock daily-candle feed lags — verified live (2026-07-18, Phase 3 T8):**
  a full collection run on Saturday 2026-07-18 still returned candles only
  through 2026-07-16 (Friday 2026-07-17's candle absent, and it was a normal
  trading day). Until the mock feed catches up, the scoring freshness gate
  (reference = last weekday strictly before today) will correctly reject runs
  on mock — this is the gate working as designed, not a pipeline bug. Re-check
  before relying on same-day candles from `mockapi.kiwoom.com`.
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
  failed (`012510` above). Idempotent rerun completed in **~2 minutes** with an
  unchanged candle count and the same single failure. (Attribution note: the
  ~2-minute rerun was measured on the original resume-skip implementation, whose
  reference date was anchored on the first symbol's response; that mechanism was
  later replaced — commit `10dfa8b` — by a calendar-derived reference
  (`market_calendar.previous_weekday` compared per symbol against a single bulk
  `CollectionStore.latest_candle_dates` query) after review found the anchor
  defective. The replacement is covered by regression tests; the measured rerun
  time is expected to carry over since the skip decision per symbol is
  unchanged in the all-fresh case, but has not been re-measured.)

Sources: https://openapi.kiwoom.com/guide/index , https://github.com/younghwan91/kiwoom-rest-api,
live verification against `mockapi.kiwoom.com` (2026-07-17, Phase 1 implementation;
2026-07-17, Phase 2 implementation including full-universe collection).

## 5b. Verified external-service facts (Phase 4 — Ollama / Naver)

Measured live during Phase 4 acceptance (2026-07-18); see
`docs/retrospectives/2026-07-18-phase4-ai-analysis.md` §3 for evidence.

- **⚠️ Naver: legacy Open API and NAVER API HUB keys are NOT interchangeable.**
  Keys issued on NAVER API HUB (secret is 40 chars) are rejected by the legacy
  `openapi.naver.com/v1/search/news.json` with 401 / errorCode 024. The adapter
  targets the API HUB: `https://naverapihub.apigw.ntruss.com/search/v1/news`,
  headers `X-NCP-APIGW-API-KEY-ID` / `X-NCP-APIGW-API-KEY`. Response body
  format is identical to the legacy API (items/title/originallink/link/
  description/pubDate, `<b>` tags + HTML entities; JSON is the default without
  a `format` param). Daily quota 25,000 calls
  (https://api.ncloud-docs.com/docs/naver-api-hub-search-news).
- **⚠️ Ollama cloud inference ignores `format: "json"`.** `gemma4:31b-cloud`
  (remote inference via ollama.com; requires `ollama signin`) wraps responses
  in markdown fences (```json ... ```) despite the format constraint. The
  adapter strips exactly one symmetric fence (backreference-matched backtick
  count) before returning; domain parsing stays strict/fail-loud. Local models
  honoring `format` pass through unchanged.
- **Container → host Ollama works with default binding.** Docker Desktop
  (Windows) proxies `host.docker.internal:11434` to the host loopback, so
  `OLLAMA_HOST=0.0.0.0` (LAN exposure) is NOT needed — keep the default
  127.0.0.1 binding. From host processes, `host.docker.internal` resolves to
  the LAN IP and is refused — host-side live smokes use `127.0.0.1`.
- **LangSmith telemetry is quadruple-blocked** (the 4 env var names the
  installed SDK recognizes are pinned "false" in docker-compose AND
  `AnalysisPipeline.__init__` raises if any is truthy) — prompts/verdicts
  must never leave for a third-party SaaS. (Cloud model inference is a
  separate, user-accepted exposure — re-evaluate before live trading.)

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
