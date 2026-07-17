# STATUS — 재개 지점 (Resume Point)

> **이 문서는 "지금 어디까지 했고, 다음에 뭘 해야 하는가"의 단일 출처입니다.**
> 새 세션에서 재개할 때 이 문서를 가장 먼저 읽으세요. 매 작업 세션이 끝날 때마다
> 이 문서를 갱신합니다(핸드오프 문서).

- **최종 수정:** 2026-07-17
- **프로젝트:** OhMyStock — 한국 주식 자동매매 시스템

---

## ▶ 여기서 재개 (다음 액션)

**Phase 0(워킹 스켈레톤) 완료 (2026-07-17). 다음은 Phase 1(키움 브로커 어댑터,
모의투자) spec 브레인스토밍.**

- Phase 0 회고록: `docs/retrospectives/2026-07-17-phase0-walking-skeleton.md`
  (Task 1~10 각 목적·파일·커밋 SHA, 설계/패턴, 겪은 문제, E2E 검증 결과, 남은
  Minor 항목 전부 기록됨).
- 진행 원장: `.superpowers/sdd/progress.md` (태스크별 커밋 SHA + 리뷰 결과 + 보류
  Minor 목록).
- E2E DoD 7개 항목 전부 통과(2026-07-17, 코디네이터 검증 + 사용자 육안 확인):
  clean `docker compose up` → `db` healthy/`backend` up, `/health` →
  `{"status":"ok","db":"ok","mode":"mock"}`, 백엔드 `uv run pytest` 9 passed,
  프론트 `pnpm test` 3 passed, Electron 창 상태 표시 확인, 백엔드 단절/복구 시
  자동 재연결 확인. 증거: `.superpowers/sdd/task-10-e2e-evidence.md`.
- 커밋 10개 메시지는 사용자가 사전 일괄 승인함(계획서의 메시지 그대로).
- ⚠️ **블로커 (Phase 1 착수 전 사용자 작업 필수):** `openapi.kiwoom.com` 가입 +
  **app key/secret 발급** + **모의투자 신청**. Phase 0에서는 "미해결 선행조건"
  (환경변수 존재만 검증, 실제 호출 없음)이었지만, Phase 1은 실제 키움 REST API를
  호출하므로 이제 **실제 블로커**다. 이 세 가지가 끝나기 전에는 Phase 1 브로커
  어댑터 구현에 착수할 수 없다(spec/plan 작성까지는 가능).
- ⚠️ 커밋 규칙(CLAUDE.md 규칙 7): 커밋 전 **메시지 전문 컨펌 필수**, 커밋 메시지에
  **AI 흔적(Co-Authored-By 등) 금지**. 기존 이력도 재작성 완료(2026-07-14).

새 세션에서 재개하려면 Claude에게 이렇게 말하세요:
> "`docs/STATUS.md` 읽고 재개 지점부터 계속해."

---

## 워크플로 진행 상황

```
[x] 브레인스토밍: 자산군, 브로커, 아키텍처, DB, 컨테이너 경계
[x] Phase 0 설계 spec 작성 + 커밋 + 사용자 승인 (2026-07-14)
[x] writing-plans: Phase 0 구현 계획서 (docs/plans/2026-07-14-phase0-walking-skeleton-plan.md)
[x] Phase 0 구현 (워킹 스켈레톤) — Task 1~10 완료, E2E DoD 7개 항목 전부 통과 (2026-07-17)
[x] Phase 0 회고록 (docs/retrospectives/2026-07-17-phase0-walking-skeleton.md)
[ ] Phase 1: 키움 브로커 어댑터 (모의투자)                <-- 다음 (사용자의 키움 가입/키
    발급/모의투자 신청 완료 후 spec 브레인스토밍부터)
... Phase 2~8 (CLAUDE.md 로드맵 참고)
```

## 결정 로그 (무엇을, 왜 정했나)

| # | 결정 | 이유 | 기록 위치 |
|---|---|---|---|
| 1 | 자산군 = **한국 주식** | 프로젝트 목표가 국내 주식 시장 자동매매 | CLAUDE.md §1 |
| 2 | 브로커 = **키움 REST API** (신) | 크로스플랫폼 REST. 구 OpenAPI+는 Windows 전용 OCX라 Electron과 비호환 | CLAUDE.md §5 |
| 3 | 아키텍처 **A**: 컨테이너 FastAPI 백엔드 + 호스트 네이티브 Electron UI | AI/퀀트/텔레그램 단일 언어 통합. 엔진이 UI 종료와 무관하게 생존 | CLAUDE.md §3 |
| 4 | 컨테이너 경계: 백엔드+DB는 docker-compose, **Electron은 호스트** | Electron은 데스크톱 GUI라 컨테이너 부적합(특히 Windows) | CLAUDE.md §3 |
| 5 | DB = **PostgreSQL** (순수) | 멀티서비스 동시 접근. TimescaleDB는 추후 추가 가능 | CLAUDE.md §3 |
| 6 | **모의투자 우선** (`mockapi.kiwoom.com`) | 안전: 자동매매를 실전 자금으로 먼저 만들지 않는다 | CLAUDE.md §4 |
| 7 | 첫 서브프로젝트 = **Phase 0 워킹 스켈레톤** | 기능 구현 전에 아키텍처를 end-to-end로 검증 | docs/specs/2026-06-16-phase0-walking-skeleton-design.md |
| 8 | 문서는 **한국어**로 작성 (CLAUDE.md만 영어) | 사용자 지시. CLAUDE.md는 규칙 6에 따라 영어 유지 | CLAUDE.md §2-1 |

## 후속 설계를 제약하는 검증된 팩트 (사용 전 재확인)

- 키움 REST에는 **네이티브 TP/SL/Stop이 없음** → **클라이언트측 구현** 필수 (Phase 5).
- 레이트리밋 **TR당 ~1 req/s** (전역 아님) → 전종목 봉 수집은 **야간 배치** (Phase 2).
- 인증 토큰 만료 → 재발급 로직 필요 (Phase 1).
- 상세·출처는 `CLAUDE.md` §5 참고.

## 미해결 선행조건 → 이제 실제 블로커 (사용자 작업, Phase 1 착수 전 필수)

Phase 0에서는 환경변수 존재 여부만 검증하고 실제 키움 API를 호출하지 않았기 때문에
"미해결 선행조건"으로만 기록해 두었다. Phase 1(브로커 어댑터)은 실제로 키움 REST
API를 호출하므로, 아래 세 가지가 없으면 **구현을 시작할 수 없다**.

- [ ] `openapi.kiwoom.com` 가입
- [ ] **app key / secret** 발급
- [ ] **모의투자** 신청

## 문서 인덱스

| 경로 | 용도 |
|---|---|
| `CLAUDE.md` | 규칙·아키텍처·검증된 API 팩트·로드맵 (매 세션 자동 로드, 영어) |
| `docs/STATUS.md` | 이 문서 — 재개 지점 + 결정 로그 |
| `docs/specs/2026-06-16-phase0-walking-skeleton-design.md` | Phase 0 설계 spec |
| `docs/architecture/system-overview.md` | 마스터 청사진 (Task 9, Phase 0 구현 중 작성) |
| `docs/plans/2026-07-14-phase0-walking-skeleton-plan.md` | Phase 0 구현 계획서 (Task 1~10) |
| `docs/retrospectives/2026-07-17-phase0-walking-skeleton.md` | Phase 0 회고록 (Task 1~10 상세, E2E 결과) |
| `docs/retrospectives/` | 작업별 회고록 (규칙 4) |

## 세션 연속성 작동 방식

1. **CLAUDE.md**는 새 세션에서 Claude Code가 자동 로드하며 이 문서를 가리킨다.
2. **이 문서(`docs/STATUS.md`)**가 사람/AI가 읽는 재개 지점이며, 세션 종료 전 항상
   마지막으로 갱신한다.
3. 모든 것이 **git**에 커밋되어 세션·기기 간에 상태가 보존된다.
4. (보조) `claude --resume` / `claude --continue`로 이전 대화 자체를 다시 열 수 있으나,
   위 문서들이 버전관리되는 견고한 단일 출처다.
