# STATUS — 재개 지점 (Resume Point)

> **이 문서는 "지금 어디까지 했고, 다음에 뭘 해야 하는가"의 단일 출처입니다.**
> 새 세션에서 재개할 때 이 문서를 가장 먼저 읽으세요. 매 작업 세션이 끝날 때마다
> 이 문서를 갱신합니다(핸드오프 문서).

- **최종 수정:** 2026-07-17
- **프로젝트:** OhMyStock — 한국 주식 자동매매 시스템

---

## ▶ 여기서 재개 (다음 액션)

**Phase 2(데이터 수집 파이프라인) 브레인스토밍 완료, spec 작성됨 — 사용자 spec
검토 대기. 승인 시 `writing-plans`로 구현 계획서 작성.**

- Phase 2 spec: `docs/specs/2026-07-17-phase2-data-collection-pipeline-design.md`
  (유니버스=전 종목, 시동=POST /collect, 섹터 매핑=키움 TR 우선+KRX 파일 대안 B,
  구현 Task 1 = 신규 TR 3종 실측 스파이크).
- ✅ **Phase 2 PRE-GATE 통과 (2026-07-17 실측):** `base_dt`는 조회 기준일 —
  비영업일은 직전 영업일 자동 보정(에러 없음), 과거 백필 가능, 미래는 오늘 클램프.
  일봉 1페이지=600봉. (`.superpowers/sdd/phase2-pregate-basedt.txt`)
- ⚠️ **PRE-GATE — Phase 5(TP/SL 로직 배포) 착수 전 반드시 먼저 확인(hard gate):**
  모의 계좌에 실제 포지션을 만든 뒤 `kt00018` 행 단위 필드(`stk_cd`/`pur_pric`/
  `cur_prc` 등)와 `avg_price` 원 단위 반올림 가정을 실측 검증해야 한다. 검증용
  라이브 테스트(`test_live_잔고_원본응답_avg_price_실측`)는 이미 존재 — 포지션만
  생기면 즉시 재실행 가능.
- Phase 1 산출물: `backend/app/domain/broker.py`(`BrokerPort` + 도메인 모델),
  `backend/app/adapters/kiwoom/`(`errors.py`/`auth.py`/`rate_limiter.py`/
  `client.py`/`broker.py`) — `app.state.broker`로 FastAPI lifespan에 통합됨.
  단위 50 passed / 라이브 6 passed(모의서버 실호출), CLAUDE.md §5에 실측 팩트 반영
  완료.
- Phase 1 회고록: `docs/retrospectives/2026-07-17-phase1-kiwoom-broker-adapter.md`
  (Task 1~9 각 목적·파일·커밋 SHA, **패널 리뷰 결과와 수정 내역**, 설계/패턴,
  겪은 문제(키 유출 사고·SecretStr 전환·모의 키 발급 이슈), 라이브 실측 결과,
  Phase 5 이관 결정, 남은 항목 전부 기록됨).
- Phase 1 spec: `docs/specs/2026-07-17-phase1-kiwoom-broker-adapter-design.md`
  (§5 표는 2026-07-17 실측 결과로 갱신됨 — 검증됨/실측 정정/보류로 분류).

(직전 마일스톤: **Phase 0 워킹 스켈레톤 완료, 2026-07-17**)

- Phase 0 회고록: `docs/retrospectives/2026-07-17-phase0-walking-skeleton.md`
  (Task 1~10 각 목적·파일·커밋 SHA, 설계/패턴, 겪은 문제, E2E 검증 결과, 남은
  Minor 항목 전부 기록됨).
- 진행 원장: `.superpowers/sdd/progress.md` (태스크별 커밋 SHA + 리뷰 결과 + 보류
  Minor 목록).
- 커밋 메시지는 사용자가 사전 일괄 승인함(계획서의 메시지 그대로).
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
[x] Phase 1: 키움 브로커 어댑터 (모의투자) — Task 1~9 완료, 단위 50 passed +
    라이브 6 passed, 실측 팩트 CLAUDE.md §5 반영 (2026-07-17)
[x] Phase 1 회고록 (docs/retrospectives/2026-07-17-phase1-kiwoom-broker-adapter.md)
[ ] Phase 2: 데이터 수집 파이프라인                        <-- 다음 (PRE-GATE:
    base_dt 비영업일 동작 실측 먼저 — 위 재개 지점 참고)
... Phase 3~8 (CLAUDE.md 로드맵 참고)
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
| 9 | KB증권 API 전환 **기각**, 키움 유지 | KB증권 핀테크스토어는 법인/제휴사 전용(사업자번호 필수) — 개인 사용 불가. 개인용 대안은 KIS뿐 | 2026-07-17 리서치, P1 spec §1 |
| 10 | P1 범위 = 인증+시세/캔들+계좌 ("필요한 것만 먼저") | 주문·실시간은 소비자(트레이딩 엔진)와 함께 Phase 5에서 | P1 spec §1 |
| 11 | 키움 통신 코드 **직접 구현** (비공식 래퍼 미사용) | 개인 유지보수 의존 리스크 회피, 자체 레이트리밋·포트 설계 정합 (규칙 2) | P1 spec §1 |
| 12 | 태스크마다 **4-에이전트 리뷰 패널** 전원 통과 후 진행 | 사용자 지시 — 개발자/트레이더/아키텍트/보안 관점 상시 검증 | CLAUDE.md 규칙 8, `.claude/agents/` |
| 13 | 4-에이전트 패널이 **8개 코드 태스크 중 7개**에서 Critical/Important 결함을 잡아 수정시킴(1개만 1차 전원승인) — 패널 프로세스(결정 #12)의 유효성이 실측으로 입증됨 | 락-sleep 전역 직렬화, 401/429 재시도 예산 혼합, silent-0 금액 필드 등 실제 결함을 코드 작성 직후 잡아냄 | `docs/retrospectives/2026-07-17-phase1-kiwoom-broker-adapter.md` §3 총괄 |
| 14 | 트레이딩 엔진 관련 정책(긴급 TR 우선순위·타임아웃)은 **Phase 1에서 설계하지 않고 Phase 5로 이관** | Phase 1은 주문을 다루지 않아 "긴급"을 정의할 도메인 지식(소비자)이 없음 — 조기 설계는 추측 기반이 됨(YAGNI). 인프라(레이트리미터 락-바깥 sleep)는 이미 이를 지원하도록 준비됨 | `docs/retrospectives/2026-07-17-phase1-kiwoom-broker-adapter.md` §6 |
| 15 | P2 유니버스 = **전 종목** (ETF/ETN 포함, 구분 필드 저장) | 사용자 결정 — 유연성 우선, 필터는 소비 단계에서 | P2 spec §1 |
| 16 | P2 수집 시동 = **HTTP API** (`POST /collect` + status) | 사용자 결정 — Phase 7 대시보드 버튼의 토대. localhost 바인딩 전제, 인증은 Phase 7 전 재평가 | P2 spec §1·§8 |
| 17 | 일봉은 1페이지(600봉)를 **그대로 upsert** (6개월로 자르지 않음) | 추가 비용 없이 오는 데이터 — 소비자가 잘라 씀. 재실행 멱등 | P2 spec §1 |
| 18 | 섹터 매핑 = 키움 TR(ka10101+ka20002) 우선, **실측 스파이크로 확정** — 불발 시 KRX 정보데이터시스템 파일 조인(대안 B) | ka20002의 "구성종목 반환"이 미검증 추정이라 코드 작성 전 실측이 최선 | P2 spec §1·§5 |

## 후속 설계를 제약하는 검증된 팩트 (사용 전 재확인)

- 키움 REST에는 **네이티브 TP/SL/Stop이 없음** → **클라이언트측 구현** 필수 (Phase 5).
- 레이트리밋 **TR당 ~1 req/s** (전역 아님, 공식 수치는 여전히 미확인 — 설정값으로
  구현됨) → 전종목 봉 수집은 **야간 배치** (Phase 2).
- 인증 토큰 만료 → 재발급 로직 구현 완료 (Phase 1, `expires_dt` 절대 KST 시각
  기반 — 실측 완료).
- **⚠️ `ka10081`(일봉) 조회는 비어 있지 않은 `base_dt`(YYYYMMDD)가 필수 — 빈 값
  거부는 실측 확인.** 어댑터는 당일(KST)을 전송 중이며, **타 날짜 허용 여부와
  비영업일 동작은 미실측** → **Phase 2 PRE-GATE** (위 재개 지점 참고).
- **⚠️ `kt00018`(잔고) 행 단위 필드와 `avg_price` 반올림은 미실측(모의 계좌 포지션
  0개)** → **Phase 5 PRE-GATE(hard gate)**, 검증용 라이브 테스트는 이미 존재.
- 상세·출처는 `CLAUDE.md` §5, `docs/retrospectives/2026-07-17-phase1-kiwoom-broker-adapter.md` 참고.

## 문서 인덱스

| 경로 | 용도 |
|---|---|
| `CLAUDE.md` | 규칙·아키텍처·검증된 API 팩트·로드맵 (매 세션 자동 로드, 영어) |
| `docs/STATUS.md` | 이 문서 — 재개 지점 + 결정 로그 |
| `docs/specs/2026-06-16-phase0-walking-skeleton-design.md` | Phase 0 설계 spec |
| `docs/specs/2026-07-17-phase1-kiwoom-broker-adapter-design.md` | Phase 1 설계 spec (§5 실측 결과로 갱신됨) |
| `docs/architecture/system-overview.md` | 마스터 청사진 (Task 9, Phase 0 구현 중 작성) |
| `docs/plans/2026-07-14-phase0-walking-skeleton-plan.md` | Phase 0 구현 계획서 (Task 1~10) |
| `docs/plans/2026-07-17-phase1-kiwoom-broker-adapter-plan.md` | Phase 1 구현 계획서 (Task 1~9) |
| `docs/retrospectives/2026-07-17-phase0-walking-skeleton.md` | Phase 0 회고록 (Task 1~10 상세, E2E 결과) |
| `docs/retrospectives/2026-07-17-phase1-kiwoom-broker-adapter.md` | Phase 1 회고록 (Task 1~9 상세, 패널 리뷰·라이브 실측 결과) |
| `docs/retrospectives/` | 작업별 회고록 (규칙 4) |

## 세션 연속성 작동 방식

1. **CLAUDE.md**는 새 세션에서 Claude Code가 자동 로드하며 이 문서를 가리킨다.
2. **이 문서(`docs/STATUS.md`)**가 사람/AI가 읽는 재개 지점이며, 세션 종료 전 항상
   마지막으로 갱신한다.
3. 모든 것이 **git**에 커밋되어 세션·기기 간에 상태가 보존된다.
4. (보조) `claude --resume` / `claude --continue`로 이전 대화 자체를 다시 열 수 있으나,
   위 문서들이 버전관리되는 견고한 단일 출처다.
