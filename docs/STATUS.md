# STATUS — Resume Point

> **This is the single source of truth for "where are we and what's next".**
> Read this first when resuming in a new session. Keep it updated at the end of
> every work session (it is the handoff document).

- **Last updated:** 2026-06-16
- **Project:** OhMyStock — automated Korean stock trading system

---

## ▶ Resume here (next action)

**Brainstorming for Phase 0 is complete. The design spec is written and committed,
and is currently AWAITING USER REVIEW.**

- If the user has approved the spec → invoke the **`writing-plans`** skill to produce
  the Phase 0 implementation plan in `docs/plans/`.
- If the user wants spec changes → edit
  `docs/specs/2026-06-16-phase0-walking-skeleton-design.md`, re-run the spec
  self-review, then re-ask for approval.

To resume in a new session, tell Claude:
> "Read docs/STATUS.md and continue from the resume point."

---

## Where we are in the workflow

```
[x] Brainstorming: asset class, brokerage, architecture, DB, container boundary
[x] Phase 0 design spec written + committed (awaiting user review)
[ ] writing-plans: Phase 0 implementation plan        <-- NEXT
[ ] Implement Phase 0 (walking skeleton)
[ ] Phase 0 retrospective
[ ] Phase 1: Kiwoom broker adapter (mock)
... Phases 2-8 (see CLAUDE.md roadmap)
```

## Decision log (what was decided and why)

| # | Decision | Why | Recorded in |
|---|---|---|---|
| 1 | **Korean stocks**, not crypto | Upbit is a crypto exchange and cannot trade stocks (fact-check) | CLAUDE.md §1 |
| 2 | Brokerage = **Kiwoom REST API** (new) | Cross-platform REST; legacy OpenAPI+ is Windows-only OCX, incompatible with Electron | CLAUDE.md §5 |
| 3 | Architecture **A**: containerized FastAPI backend + host-native Electron UI | Single-language AI/quant/telegram stack; engine survives UI being closed | CLAUDE.md §3 |
| 4 | Container boundary: backend + DB in docker-compose; **Electron on host** | Electron is a desktop GUI; not viable in a container (esp. Windows) | CLAUDE.md §3 |
| 5 | Database = **PostgreSQL** (plain) | Concurrent multi-service access; TimescaleDB can be added later | CLAUDE.md §3 |
| 6 | **Mock-first** (`mockapi.kiwoom.com`) | Safety: never build/test auto-trading against real money first | CLAUDE.md §4 |
| 7 | First sub-project = **Phase 0 walking skeleton** | De-risk architecture end-to-end before feature work | docs/specs/2026-06-16-phase0-walking-skeleton-design.md |

## Verified facts that constrain later design (re-verify before relying)

- Kiwoom REST has **no native TP/SL/Stop** → must be implemented **client-side**
  (Phase 5).
- Rate limit **~1 req/s per TR** (not global) → all-symbol candle collection is an
  **overnight batch** (Phase 2).
- Auth token expires → reissue logic needed (Phase 1).
- Full details + sources in `CLAUDE.md` §5.

## Open prerequisites (user action, needed for Phase 1 — not Phase 0)

- Register at `openapi.kiwoom.com`, obtain **app key / secret**, apply for **mock
  trading**.

## Document index

| Path | Purpose |
|---|---|
| `CLAUDE.md` | Rules, architecture, verified API facts, roadmap (auto-loaded each session) |
| `docs/STATUS.md` | This file — resume point + decision log |
| `docs/specs/2026-06-16-phase0-walking-skeleton-design.md` | Phase 0 design spec |
| `docs/architecture/system-overview.md` | Master blueprint (created during Phase 0 impl — not yet present) |
| `docs/plans/` | Implementation plans (Phase 0 plan = next deliverable) |
| `docs/retrospectives/` | Per-task retrospectives (rule 4) |

## How session continuity works here

1. **CLAUDE.md** is auto-loaded by Claude Code in every new session and points to
   this file.
2. **This file (`docs/STATUS.md`)** is the human/AI-readable resume point — always
   updated last before ending a session.
3. Everything is committed to **git**, so the state survives across sessions and
   machines.
4. (Optional, complementary) `claude --resume` / `claude --continue` can reopen the
   actual prior conversation, but the docs above are the durable, version-controlled
   source of truth.
