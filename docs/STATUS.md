# STATUS — 재개 지점 (Resume Point)

> **이 문서는 "지금 어디까지 했고, 다음에 뭘 해야 하는가"의 단일 출처입니다.**
> 새 세션에서 재개할 때 이 문서를 가장 먼저 읽으세요. 매 작업 세션이 끝날 때마다
> 이 문서를 갱신합니다(핸드오프 문서).

- **최종 수정:** 2026-07-18
- **프로젝트:** OhMyStock — 한국 주식 자동매매 시스템

---

## ▶ 여기서 재개 (다음 액션)

**Phase 5 사전 게이트 정비 완료(2026-07-18) — Phase 5(트레이딩 엔진)
스펙 브레인스토밍부터 재개.** PRE-GATE 코드 게이트 4건(#2 advice 저장,
#4 타임스탬프/기준일, #5 비용 연동/한계 명시, #6 쓰기 보호)은 전부 해소
— 회고록 `docs/retrospectives/2026-07-18-phase5-pregate-hardening.md`.
잔여: **#1 kt00018 실포지션 실측은 주문 TR 구현이 선행돼야 하므로
Phase 5 본편 초반 태스크로 편입**(장중 실행 필요), #7 외부 추론 재평가는
실전 전환 게이트, #8 Electron Origin 실측은 Phase 7 전. Phase 5 스펙에
반영할 이월: 주문 엔드포인트 별도 스코프 토큰, 실비용 SSOT 경계,
economist 폴백 발동 구분 필드, 실패 런 이력 GET (회고록 §5). 테스트
현황 **319 passed, 10 deselected**.

- **Phase 4 산출물:** `backend/app/domain/analysis/`(ports·config·parsing·
  prompts·graph(LangGraph)·service), `backend/app/adapters/{ollama,naver}/`,
  `backend/app/store/analysis_store.py`(+Alembic `0005` — analysis_runs/
  verdicts/news 3테이블), `backend/app/api/analyze.py`(`POST /analyze` +
  status/latest), `backend/app/core/background_service.py`(선행 리팩터 —
  3서비스 공통 스캐폴딩). 테스트 **299 passed**(10 deselected 라이브).
- **✅ 수용 검증 (2026-07-18, 실환경):** 실데이터 end-to-end 완주 —
  후보 18종목, **65초**, 뉴스 103건 저장, regime=risk_off, approve 1/
  reject 17, 픽 0(economist advice=0 — 설계 의도). **LLM 인용 수치 전수
  DB 대조 일치(환각 없음).** 증거: `.superpowers/sdd/p4-task-7-*`.
- **⚠️ 실환경 수용 검증에서 발견·수정된 어댑터 가정 2건**(단위 테스트로는
  못 잡는 클래스): ① 네이버 구 오픈 API ↔ **API HUB 키 비호환**(401/024)
  → API HUB 엔드포인트·헤더로 마이그레이션, ② **클라우드 추론 경로는
  `format:"json"` 무시**하고 마크다운 펜스로 감쌈 → 어댑터가 대칭 펜스
  한 겹 벗김(도메인 파싱은 엄격 유지). P4 회고록 §3.
- **⚠️ AI 분석 운영 노트:** 컨테이너 → 호스트 Ollama는
  `host.docker.internal`로 정상(Docker Desktop 게이트웨이가 루프백 프록시)
  — **`OLLAMA_HOST=0.0.0.0`(LAN 노출) 불필요, 기본 바인딩 유지가 보안상
  정답.** LangSmith 텔레메트리 4개 env var는 compose에서 "false" 고정 +
  파이프라인 생성 시 RuntimeError 가드. 클라우드 모델은 `ollama signin`
  선행 필요. 네이버 API HUB 일 25,000회 한도.
- Phase 4 회고록: `docs/retrospectives/2026-07-18-phase4-ai-analysis.md`
  (태스크별 커밋·패널 결함, 수용 검증 실측, 이월 게이트, 스펙 충돌 지적
  1건 — 사용자 결정 대기).
- Phase 4 spec: `docs/specs/2026-07-18-phase4-ai-analysis-design.md`.

- Phase 3 산출물: `backend/app/domain/scoring/`(config·indicators·strategies·
  simulation·engine·service), `backend/app/domain/sector_classification.py`,
  `backend/app/store/scoring_store.py`(+Alembic `0003`/`0004` — 멤버십 다대다,
  상태 필드, 스코어링 4테이블), `backend/app/api/score.py`(`POST /score` +
  status/latest). 테스트 **187 passed** (8 deselected 라이브).
- **✅ 수용 검증 (2026-07-18, 모의서버 실데이터):** 재수집 65분(3,886/3,887),
  **industry 중복 소속률 0.00%**(기준 <5%), 프로덕션 신선도 게이트가 낡은
  데이터 정확 거부 실증, 기준일 주입 실행으로 end-to-end 성공 — 유니버스
  2,514, **계산 9.3초**, 선정 업종 5(금융 포함 — T1 재분류 수정의 실증) /
  후보 18종목. 증거: `.superpowers/sdd/p3-task-8-*`.
- **⚠️ 스코어링 운영 노트:** 모의서버 일봉 피드는 지연됨(토 7/18 수집에도
  최신 봉 7/16) — 피드가 따라잡기 전 모의 환경 야간 스코어링은 게이트에서
  실패하는 것이 정상. 수집↔스코어링은 도메인 레벨 상호 배제(conflict_check)
  + API 409 양방향 가드.
- Phase 3 회고록: `docs/retrospectives/2026-07-18-phase3-scoring-engine.md`
  (태스크별 커밋, 패널 핵심 결함 8건, 프로세스 사건 4건, 백로그, 실측 통계).
- Phase 3 spec: `docs/specs/2026-07-18-phase3-scoring-engine-design.md`
  (§4-3/§4-4-b 한계 명문화, §9 이후 연계, §10 무인증 쓰기 리스크).

(직전 마일스톤 기록 — Phase 2 완료 시점 상태:)

- Phase 2 산출물: `backend/app/domain/collection.py`(`CollectionService`),
  `backend/app/store/{models.py,collection_store.py}`(4개 테이블 + upsert
  리포지토리, Alembic `0002`), `backend/app/adapters/kiwoom/broker.py`
  (`list_instruments`/`list_sectors`/`list_sector_members`),
  `backend/app/api/collect.py`(`POST /collect` + `GET /collect/status`),
  `backend/app/core/market_calendar.py`. 단위 98 passed(8 deselected 라이브),
  풀 수집 실측 완주(아래).
- **✅ 풀 수집 실측 완료 (2026-07-17, 모의서버):** 3,887개 종목(3,886 성공/1
  실패), 약 67분, 캔들 2,120,535행. 재실행(스킵) 약 2분, 결과 동일 — 멱등성
  확인. (`.superpowers/sdd/p2-task-7-collect-monitor.txt`,
  `p2-task-7-db-verify.txt`, `p2-task-7-rerun2-monitor.txt`)
- **⚠️ 수집 운영 노트(Phase 6 스케줄러 설계 시에도 유지):**
  1. **수집은 19시(KST) 이후 실행 권장** — 장 마감 직후는 당일봉이 아직
     확정 전일 수 있음(정확한 확정 시각은 미실측, PRE-GATE `base_dt` 자동
     보정은 이미 실측 확인됨).
  2. **백엔드 컨테이너 가동 중에는 호스트/별도 프로세스에서 키움 토큰을
     발급하지 말 것** — 앱키당 활성 토큰이 1개뿐인 것으로 추정되며(미확정,
     측정 정황 근거), 다른 프로세스의 토큰 발급이 백엔드의 진행 중인 토큰을
     무효화해 `[8005]` 오류를 유발한 사고가 실제로 있었다(CLAUDE.md §5,
     `docs/retrospectives/2026-07-17-phase2-data-collection-pipeline.md` §5).
- ✅ **Phase 2 PRE-GATE 통과 (2026-07-17 실측):** `base_dt`는 조회 기준일 —
  비영업일은 직전 영업일 자동 보정(에러 없음), 과거 백필 가능, 미래는 오늘 클램프.
  일봉 1페이지=600봉. (`.superpowers/sdd/phase2-pregate-basedt.txt`)
- ⚠️ **PRE-GATE — Phase 5(트레이딩 엔진) 관련 (2026-07-18 사전 게이트
  정비로 #2~#6 해소 — 회고록 참고):**
  1. **`kt00018` 행 단위 필드 실측 — Phase 5 본편 초반 태스크로 편입.**
     주문 TR(kt10000) 구현 후 모의 계좌에 포지션을 만들어야 실측 가능
     (장중 실행 필요). `stk_cd`/`pur_pric`/`cur_prc`와 `avg_price` 원 단위
     반올림 검증. 라이브 테스트(`test_live_잔고_원본응답_avg_price_실측`)
     기존재.
  2. ~~`max_picks_advice` DB 저장~~ — **해소(`5a10a2f`, Alembic 0006).**
  3. ~~economist 폴백 사용자 결정~~ — **종결: 현행 유지(열림), 결정 #23.**
  4. ~~`/analyze/status` 타임스탬프 + `score_reference_date`~~ —
     **해소(`5a10a2f`).**
  5. ~~프롬프트 비용 연동 + 백테스트 한계 명시~~ — **해소(`8ae8ac8` +
     `b349bcd` — 인스턴스 렌더링으로 주입 cfg가 프롬프트 구동).**
  6. ~~쓰기 엔드포인트 인증/CORS~~ — **해소(`baaef00` + `0350323` —
     X-API-Key + CORS allowlist, 실전 모드 토큰 필수는 Settings validator로
     코드 강제).**
  7. AI 추론의 외부(Ollama Cloud) 처리 수용 재평가 — 실전 전환 시 로컬
     모델 회귀가 기본 계획 (P4 spec §10-5).
  8. Phase 7(Electron 대시보드) 착수 전 — 패키징된 Electron 렌더러의 실제
     Origin(`file://` → null 가능) 실측 후 `CORS_ORIGINS` 값/방식 재확정
     (P5pre-T3 아키텍트 패널).
  9. Phase 5 스펙 브레인스토밍에 반영할 이월(사전 게이트 회고록 §5):
     주문 엔드포인트 **별도 스코프 토큰**, 실비용 SSOT 경계(프롬프트
     서술용 근사 vs 체결비용 계산), economist 폴백 발동 구분 필드,
     실패 런 이력 조회 GET, `/score/latest`·`/analyze/latest` 무인증
     신호 조회 재평가. Phase 6 전: progress 타임스탬프의
     BackgroundRunService 승격(3서비스 대칭).
- ✅ **PRE-GATE — Phase 3(스코어링) 3건: 2026-07-18 라이브 프로브로 실측 완료.**
  정책 결정만 Phase 3 브레인스토밍에 남음. 증거:
  `.superpowers/sdd/p3-pregate-sectors.txt`(1차 — 페이지네이션 누락으로 100행
  캡, 참고용), `p3-pregate-sectors-paged.txt`(페이지네이션 포함 재실측, 확정),
  `p3-pregate-state.txt`(ka10099 필드 분포).
  1. **집계 업종 필터 — 실측 결과 현행 필터(001/101+이름 마커)로는 불충분.**
     65개 업종 전수 멤버 수 실측: 001=2,477·101=1,821(코스닥 전체)로 집계 확정.
     그러나 업종 목록 자체가 이질적 — 산업 업종 외에 **규모 그룹**(002/003/004
     대·중·소형주, 138/139/140 KOSDAQ 100/MID/SMALL), **등급 그룹**(142~145
     우량/벤처/중견/신성장), **지수 멤버십**(150/151, 603~605, 160/165), **우산
     업종**(kospi 021 금융 ⊇ 증권 024/보험 025 추정, 027 제조 ⊇ 제조 하위업종
     추정)이 섞여 있고, 한 종목이 여러 그룹에 중복 소속된다. 50% 카나리는
     kosdaq 106 제조(1,116명=61%)·140 KOSDAQ SMALL(1,346명=74%)을 오탐.
     **현행 수집 매핑은 last-write-wins 단일 sector_code라 순회 순서에 따라
     산업 업종 배정이 등급 그룹으로 덮어써짐** (DB의 140=159명 vs 실측
     1,346명 불일치가 증거) → **Phase 3에서 산업 업종 화이트리스트 확정 +
     섹터 매핑 재설계(스코어링용 분류 체계) 필요.**
  2. **ETF/보통주 구분 소비 정책 — 판별 수단은 확정, 정책만 미결.** DB 실측:
     kospi 919 / kosdaq 1,821 / etf 1,147 (전 종목 kind="A" — 구분은 `market`
     컬럼으로 신뢰 가능). ETN 등 기타 marketCode 6종 412행은 현재 미수집.
     스코어링 유니버스에 ETF 포함 여부 + 412행 수집 여부는 브레인스토밍 결정.
  3. **관리종목/거래정지 필드 — 판별 가능 확정.** ka10099 실측 분포:
     `auditInfo` ∈ {정상, 거래정지, 관리종목, 투자주의, 투자경고, 단기과열,
     투자주의환기종목}, `state`는 증거금율+플래그의 파이프 결합(예:
     "증거금100%|거래정지", "관리종목"). 비정상 종목 실측: kospi 64 / kosdaq
     169 / etf 6. 현재 Instrument는 이를 저장하지 않음 → Phase 3에서 도메인
     모델 확장(저장) + 스코어링 유니버스 제외 필터로 도입할 것.
- ⚠️ **Phase 3 요구사항 (2026-07-18 추가): 신선도 게이트** — 스코어링 실행 전
  "전 대상 종목의 최근 봉 날짜 ≥ 직전 영업일"을 검증하고 미충족 시 계산을
  거부(또는 수집 선행 요구)한다. 저장소는 read-through가 아니므로 수집 공백
  시 조회는 에러 없이 낡은 데이터를 반환한다 — 낡은 봉 기반 매매 신호가
  조용히 나가는 것을 구조적으로 차단해야 함.
- Phase 2 회고록: `docs/retrospectives/2026-07-17-phase2-data-collection-pipeline.md`
  (Task 1~7 각 목적·파일·커밋 SHA, 패널 리뷰 결과와 수정 내역, 설계/패턴,
  프로세스 사건 3건(증거 파일명 충돌·자체 패널 실행 금지·코디네이터 주장
  정정) 정직 기록, 풀 수집 실측 결과, 남은 항목 전부 기록됨).
- Phase 2 spec: `docs/specs/2026-07-17-phase2-data-collection-pipeline-design.md`
  (§5 스파이크 대상 → 실측 결과 표로 갱신, §8 리스크에 8005/단일 토큰 항목
  반영, 상태 "확정 — 구현 완료").
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
[x] Phase 2: 데이터 수집 파이프라인 — Task 1~7 완료, 풀 수집 실측(3,887종목/
    67분/캔들 212만행) + 재실행 멱등 확인, 실측 팩트 CLAUDE.md §5 반영
    (2026-07-17)
[x] Phase 2 회고록 (docs/retrospectives/2026-07-17-phase2-data-collection-pipeline.md)
[x] Phase 3 PRE-GATE 3건 라이브 프로브 실측 (2026-07-18)
[x] Phase 3: 스코어링 엔진 — Task 1~8 완료, 187 passed, 실데이터 수용 검증
    통과 (2026-07-18)
[x] Phase 3 회고록 (docs/retrospectives/2026-07-18-phase3-scoring-engine.md)
[x] 선행 리팩터: BackgroundRunService 추출 (P3 spec §9 — 2026-07-18)
[x] Phase 4: AI 멀티에이전트 분석 — Task 1~7 완료, 299 passed, 실환경
    end-to-end 수용 검증 통과(65초/18종목/환각 없음) (2026-07-18)
[x] Phase 4 회고록 (docs/retrospectives/2026-07-18-phase4-ai-analysis.md)
[x] Phase 5 사전 게이트 정비 — 코드 게이트 4건 해소, 319 passed
    (docs/retrospectives/2026-07-18-phase5-pregate-hardening.md, 2026-07-18)
[ ] Phase 5: 트레이딩 엔진 스펙 브레인스토밍                  <-- 다음
    (스펙에 반영: PRE-GATE #9 이월 목록. 본편 초반: kt00018 실측 태스크)
... Phase 6~8 (CLAUDE.md 로드맵 참고)
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
| 16 | P2 수집 시동 = **HTTP API** (`POST /collect` + status) | 사용자 결정 — Phase 7 대시보드 버튼의 토대. localhost 바인딩 전제, `POST /collect`·`POST /score` 둘 다 무인증 쓰기 — CORS `allow_origins=["*"]` 하에서는 drive-by 트리거 가능. Phase 5·Phase 7 전 인증/CORS 오리진 제한 재평가 필요(T7 패널 보안) | P2 spec §1·§8, P3 spec §10 |
| 17 | 일봉은 1페이지(600봉)를 **그대로 upsert** (6개월로 자르지 않음) | 추가 비용 없이 오는 데이터 — 소비자가 잘라 씀. 재실행 멱등 | P2 spec §1 |
| 18 | 섹터 매핑 = 키움 TR(ka10101+ka20002) 우선, **실측 스파이크로 확정** — 불발 시 KRX 정보데이터시스템 파일 조인(대안 B) | ka20002의 "구성종목 반환"이 미검증 추정이라 코드 작성 전 실측이 최선 | P2 spec §1·§5 |
| 19 | 무효 토큰 응답(HTTP 200 + `[8005]`)은 기존 401 재발급 경로와 **동일한 1회성 invalidate-and-reissue 분기**로 처리(별도 정책 신설 안 함) | 401과 본질이 같은 "토큰이 더 이상 유효하지 않다"는 신호 — 별도 상태기계를 만들면 재시도 예산 이중관리 위험(Phase 1 Task 5의 401/429 혼합 버그와 동일 클래스) | CLAUDE.md §5, `backend/app/adapters/kiwoom/client.py`(commit `50391ac`) |
| 20 | P4 LLM = **gemma4:31b-cloud**(Ollama 클라우드 추론) — 외부 전송 수용, 단 **로컬 모델 전환은 설정만으로 가능**하게 유지 + 실전 전환 전 재평가 필수 | 사용자 결정(로컬 하드웨어 제약). LangSmith 텔레메트리는 별도로 4중 차단 | P4 spec §10-5, `docker-compose.yml`, `backend/app/domain/analysis/graph.py` |
| 21 | 뉴스 = **네이버 API HUB**(신규 플랫폼) — 구 오픈 API 아님 | 사용자가 발급한 키가 API HUB용이고 두 체계는 키 비호환(401/024 실측). 응답 형식은 동일해 어댑터 상수만 교체 | P4 회고록 §3-1, `backend/app/adapters/naver/client.py` |
| 22 | LLM 응답의 마크다운 펜스 제거는 **어댑터 소관**(도메인 파싱은 엄격 유지) | 펜스는 특정 벤더/경로의 전송 아티팩트 — 도메인이 알면 "domain은 외부 의존 금지" 위반. 대칭 펜스 한 겹만 벗겨 fail-loud 유지 | P4 회고록 §3-2, P4-T7 아키텍트 패널 |
| 23 | economist 파싱 실패 폴백 = **현행 유지(중립 + 상한 5, 열림)** — 트레이더 패널의 "닫힘(상한 0)" 권고를 사용자가 기각 | 사용자 결정(2026-07-18): 파이프라인 가용성 우선. 종목별 판정은 계속 기록되므로 감사는 가능. P4 회고록 §7 충돌 종결 | P4 spec §8(현행 유지), 본 결정 로그 |
| 24 | 쓰기 엔드포인트 보호 = **API 키 헤더(X-API-Key) + CORS 오리진 제한** | 사용자 결정(2026-07-18). 토큰 미설정 시 경고만(모의 로컬 개발 편의), 실전 전환 게이트에서 필수로 승격 | P5 사전 게이트 계획서 Task 3 |

## 후속 설계를 제약하는 검증된 팩트 (사용 전 재확인)

- 키움 REST에는 **네이티브 TP/SL/Stop이 없음** → **클라이언트측 구현** 필수 (Phase 5).
- 레이트리밋 **TR당 ~1 req/s** (전역 아님, 공식 수치는 여전히 미확인 — 설정값으로
  구현됨) → 전종목 봉 수집은 **야간 배치**, Phase 2에서 **약 67분/3,887종목**으로
  실측 확인.
- 인증 토큰 만료 → 재발급 로직 구현 완료 (Phase 1, `expires_dt` 절대 KST 시각
  기반 — 실측 완료). **Phase 2 실측 추가:** 무효 토큰은 HTTP 401이 아니라
  **HTTP 200 + `[8005]`**로 응답 — 재발급 분기 추가로 대응(결정 #19). 앱키당
  활성 토큰 1개로 추정(미확정) → 백엔드 가동 중 별도 프로세스 토큰 발급 금지.
- **`ka10081`(일봉) 조회는 비어 있지 않은 `base_dt`(YYYYMMDD)가 필수** — 빈 값
  거부는 실측 확인. **Phase 2 PRE-GATE로 추가 실측 완료:** 비영업일은 직전
  영업일로 자동 보정(에러 없음), 과거 백필 가능, 미래는 오늘로 클램프.
- **⚠️ `kt00018`(잔고) 행 단위 필드와 `avg_price` 반올림은 미실측(모의 계좌 포지션
  0개)** → **Phase 5 PRE-GATE(hard gate)**, 검증용 라이브 테스트는 이미 존재.
- **Phase 3 PRE-GATE 3건 실측 완료(2026-07-18)** — 업종 목록은 산업/규모/등급/
  지수/우산 그룹이 혼재된 중복 소속 구조(현행 단일 sector_code 매핑은
  last-write-wins로 손실적), 관리종목·거래정지는 `auditInfo`/`state`로 판별
  가능. 정책 결정(산업 업종 화이트리스트·ETF 포함·필드 저장)은 Phase 3
  브레인스토밍에서 — 위 재개 지점 참고.
- 상세·출처는 `CLAUDE.md` §5, `docs/retrospectives/2026-07-17-phase1-kiwoom-broker-adapter.md`,
  `docs/retrospectives/2026-07-17-phase2-data-collection-pipeline.md` 참고.

## 문서 인덱스

| 경로 | 용도 |
|---|---|
| `CLAUDE.md` | 규칙·아키텍처·검증된 API 팩트·로드맵 (매 세션 자동 로드, 영어) |
| `docs/STATUS.md` | 이 문서 — 재개 지점 + 결정 로그 |
| `docs/specs/2026-06-16-phase0-walking-skeleton-design.md` | Phase 0 설계 spec |
| `docs/specs/2026-07-17-phase1-kiwoom-broker-adapter-design.md` | Phase 1 설계 spec (§5 실측 결과로 갱신됨) |
| `docs/specs/2026-07-17-phase2-data-collection-pipeline-design.md` | Phase 2 설계 spec (§5·§8 실측 결과로 갱신됨) |
| `docs/architecture/system-overview.md` | 마스터 청사진 (Task 9, Phase 0 구현 중 작성) |
| `docs/plans/2026-07-14-phase0-walking-skeleton-plan.md` | Phase 0 구현 계획서 (Task 1~10) |
| `docs/plans/2026-07-17-phase1-kiwoom-broker-adapter-plan.md` | Phase 1 구현 계획서 (Task 1~9) |
| `docs/plans/2026-07-17-phase2-data-collection-pipeline-plan.md` | Phase 2 구현 계획서 (Task 1~7) |
| `docs/retrospectives/2026-07-17-phase0-walking-skeleton.md` | Phase 0 회고록 (Task 1~10 상세, E2E 결과) |
| `docs/retrospectives/2026-07-17-phase2-data-collection-pipeline.md` | Phase 2 회고록 (Task 1~7 상세, 패널 리뷰·풀 수집 실측 결과·프로세스 사건 3건) |
| `docs/retrospectives/2026-07-17-phase1-kiwoom-broker-adapter.md` | Phase 1 회고록 (Task 1~9 상세, 패널 리뷰·라이브 실측 결과) |
| `docs/specs/2026-07-18-phase3-scoring-engine-design.md` | Phase 3 설계 spec |
| `docs/specs/2026-07-18-phase4-ai-analysis-design.md` | Phase 4 설계 spec (모델·뉴스 결정 반영) |
| `docs/plans/2026-07-18-phase4-ai-analysis-plan.md` | Phase 4 구현 계획서 (Task 1~7) |
| `docs/retrospectives/2026-07-18-phase3-scoring-engine.md` | Phase 3 회고록 |
| `docs/retrospectives/2026-07-18-phase4-ai-analysis.md` | Phase 4 회고록 (패널 결함·수용 검증 실측·이월 게이트) |
| `docs/plans/2026-07-18-phase5-pregate-hardening-plan.md` | Phase 5 사전 게이트 정비 계획서 (Task 1~3) |
| `docs/retrospectives/2026-07-18-phase5-pregate-hardening.md` | Phase 5 사전 게이트 정비 회고록 (게이트 해소 내역·이월) |
| `docs/retrospectives/` | 작업별 회고록 (규칙 4) |

## 세션 연속성 작동 방식

1. **CLAUDE.md**는 새 세션에서 Claude Code가 자동 로드하며 이 문서를 가리킨다.
2. **이 문서(`docs/STATUS.md`)**가 사람/AI가 읽는 재개 지점이며, 세션 종료 전 항상
   마지막으로 갱신한다.
3. 모든 것이 **git**에 커밋되어 세션·기기 간에 상태가 보존된다.
4. (보조) `claude --resume` / `claude --continue`로 이전 대화 자체를 다시 열 수 있으나,
   위 문서들이 버전관리되는 견고한 단일 출처다.
