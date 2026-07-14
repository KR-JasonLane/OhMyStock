# STATUS — 재개 지점 (Resume Point)

> **이 문서는 "지금 어디까지 했고, 다음에 뭘 해야 하는가"의 단일 출처입니다.**
> 새 세션에서 재개할 때 이 문서를 가장 먼저 읽으세요. 매 작업 세션이 끝날 때마다
> 이 문서를 갱신합니다(핸드오프 문서).

- **최종 수정:** 2026-07-14
- **프로젝트:** OhMyStock — 한국 주식 자동매매 시스템

---

## ▶ 여기서 재개 (다음 액션)

**Phase 0 spec 승인됨(2026-07-14). 구현 계획서 작성·커밋 완료. 다음은 계획서 실행.**

- 계획서: `docs/plans/2026-07-14-phase0-walking-skeleton-plan.md` (Task 1~10, TDD)
- 사용자 결정(2026-07-14): 구현은 **다음 세션에서 실행**. 재개 시
  **`subagent-driven-development`**(권장) 또는 **`executing-plans`** 스킬로 계획서를
  태스크 단위 실행한다.
- ⚠️ 커밋 규칙 변경(CLAUDE.md 규칙 7): 커밋 전 **메시지 전문 컨펌 필수**, 커밋
  메시지에 **AI 흔적(Co-Authored-By 등) 금지**. 기존 이력도 재작성 완료(2026-07-14).

새 세션에서 재개하려면 Claude에게 이렇게 말하세요:
> "`docs/STATUS.md` 읽고 재개 지점부터 계속해."

---

## 워크플로 진행 상황

```
[x] 브레인스토밍: 자산군, 브로커, 아키텍처, DB, 컨테이너 경계
[x] Phase 0 설계 spec 작성 + 커밋 + 사용자 승인 (2026-07-14)
[x] writing-plans: Phase 0 구현 계획서 (docs/plans/2026-07-14-phase0-walking-skeleton-plan.md)
[ ] Phase 0 구현 (워킹 스켈레톤)                <-- 다음
[ ] Phase 0 회고록
[ ] Phase 1: 키움 브로커 어댑터 (모의투자)
... Phase 2~8 (CLAUDE.md 로드맵 참고)
```

## 결정 로그 (무엇을, 왜 정했나)

| # | 결정 | 이유 | 기록 위치 |
|---|---|---|---|
| 1 | **한국 주식** (암호화폐 아님) | Upbit은 암호화폐 거래소라 주식 매매 불가 (팩트체크) | CLAUDE.md §1 |
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

## 미해결 선행조건 (사용자 작업, Phase 1에 필요 — Phase 0엔 불필요)

- `openapi.kiwoom.com` 가입, **app key / secret** 발급, **모의투자** 신청.

## 문서 인덱스

| 경로 | 용도 |
|---|---|
| `CLAUDE.md` | 규칙·아키텍처·검증된 API 팩트·로드맵 (매 세션 자동 로드, 영어) |
| `docs/STATUS.md` | 이 문서 — 재개 지점 + 결정 로그 |
| `docs/specs/2026-06-16-phase0-walking-skeleton-design.md` | Phase 0 설계 spec |
| `docs/architecture/system-overview.md` | 마스터 청사진 (Phase 0 구현 시 생성 — 아직 없음) |
| `docs/plans/2026-07-14-phase0-walking-skeleton-plan.md` | Phase 0 구현 계획서 (Task 1~10) |
| `docs/retrospectives/` | 작업별 회고록 (규칙 4) |

## 세션 연속성 작동 방식

1. **CLAUDE.md**는 새 세션에서 Claude Code가 자동 로드하며 이 문서를 가리킨다.
2. **이 문서(`docs/STATUS.md`)**가 사람/AI가 읽는 재개 지점이며, 세션 종료 전 항상
   마지막으로 갱신한다.
3. 모든 것이 **git**에 커밋되어 세션·기기 간에 상태가 보존된다.
4. (보조) `claude --resume` / `claude --continue`로 이전 대화 자체를 다시 열 수 있으나,
   위 문서들이 버전관리되는 견고한 단일 출처다.
