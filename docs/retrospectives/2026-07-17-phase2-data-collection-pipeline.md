# 회고록 — Phase 2: 데이터 수집 파이프라인

- **작업 기간:** 2026-07-17 (단일 세션, Phase 1에 이어 연속 진행)
- **완료일:** 2026-07-17
- **근거 문서:** `docs/specs/2026-07-17-phase2-data-collection-pipeline-design.md`
  (설계 spec, §5·§8 실측 결과로 갱신됨), `docs/plans/2026-07-17-phase2-data-collection-pipeline-plan.md`
  (구현 계획서, Task 1~7)
- **커밋 범위:** `e19a541`(계획서) ~ `1a303d5`(Task 7 DBeaver 포트) + 본 문서를
  만드는 이 커밋(Task 7 문서화)

이 문서는 비전문가도 "무엇을 왜 어떻게 했는지" 따라올 수 있도록 CLAUDE.md 규칙 4에
따라 작성한다. 수치는 전부 `.superpowers/sdd/progress.md`(원장)와 실측 증거
파일을 원본으로 삼았다 — 원장과 이 문서가 어긋나면 **원장이 우선**이다(코디네이터
지시사항).

---

## 1. 무엇이 요청되었나

Phase 1이 만든 `BrokerPort`/`KiwoomBroker` 위에, **장 마감 후 전 종목의 명부·업종
매핑·일봉을 PostgreSQL에 적재하는 파이프라인**을 만드는 것이 Phase 2의 목표였다
(spec §2). 핵심 결정 3가지(spec §1, STATUS.md 결정 로그 #15~18):

1. **유니버스 = 전 종목**(ETF/ETN 포함) — 구분 필드는 저장하되 필터는 소비
   단계(Phase 3+)에서.
2. **시동 = HTTP API**(`POST /collect` + `GET /collect/status`) — Phase 7 대시보드
   버튼의 토대, Phase 6 스케줄러가 동일 함수를 내부 호출.
3. **섹터 매핑 = 키움 TR 우선(ka10101+ka20002), 실측 스파이크로 확정** — 불발 시
   대안 B(KRX 정보데이터시스템 파일 조인)로 전환.

**범위 밖(명시):** 스코어링(Phase 3), 자동 스케줄링(Phase 6), 분봉/주봉/틱,
실시간 시세, UI(Phase 7), 수집 데이터 정합성 리포트 자동화(spec §2).

## 2. 시작 시 코드 상태

Phase 1이 끝난 시점(`ee312f1`)의 백엔드는 `BrokerPort`의 4개 메서드(시세·일봉·
예수금·잔고)만 구현돼 있었고, `app.state.broker`로 lifespan에 통합돼 있었다.
`store/`에는 Phase 0의 `0001` 마이그레이션(헬스체크용 테이블 1개)만 있었고
`instruments`/`sectors`/`candles`/`collection_runs` 등 시장 데이터 테이블은
전혀 없었다. `adapters/kiwoom/errors.py`는 여전히 어댑터 계층에 있어, `domain/`이
브로커 에러 타입을 참조하려면 계층 경계를 위반해야 하는 상태였다(Task 2가
해결).

## 3. Task 1~7 — 무엇을 만들었나 (목적 · 파일 · 커밋 · 패널 리뷰)

### Task 1 — TR 3종 실측 스파이크 (커밋 없음)
**목적:** 섹터 매핑 전략(분기 A/B)을 결정하고, Task 4의 필드명을 실측으로 확정한다.
**파일:** 없음(프로브 스크립트 실행만). 산출물: `.superpowers/sdd/phase2-spike-tr.txt`.
**결과:** ka20002가 구성종목을 정상 반환 → **분기 A(키움 TR 벌크 경로) 채택**,
대안 B 불필요. 실측 필드명은 spec §5 표에 반영(§4 아래 "설계·패턴" 참고). 코드
변경이 없어 4-에이전트 패널 대상이 아니었다(계획서에 "커밋 없음"으로 명시).

### Task 2 — 하드닝 스위프 + 에러 계층 domain 이동
**목적:** Phase 1 최종 리뷰가 이월한 소소한 항목(429 백오프, `aclose` try/finally,
`expires_dt` 파싱 가드, conftest 통합)을 정리하고, `BrokerError` 계층을
`adapters/kiwoom/errors.py` → `domain/errors.py`로 옮겨 "포트 계약의 일부는
domain이 소유한다"는 원칙을 코드로 강제한다.
**파일:** `backend/app/adapters/kiwoom/{auth.py,client.py,broker.py}`,
`backend/app/domain/errors.py`(신규, `errors.py` 이동), `backend/tests/conftest.py`
(신규), `backend/tests/kiwoom/{test_auth.py,test_broker_account.py,test_client.py}`,
`backend/tests/live/test_live_smoke.py`. 9개 파일, +121/-42줄.
**커밋:** `17acd49 chore(kiwoom): hardening sweep + move broker errors to domain`
(원본 `1a915ec` → 패널 수정 후 amend)
**패널 리뷰:** sec 승인, arch 승인(+ApiError 벤더필드 docstring 권고 수용),
**dev·trader가 수정 요구 → 반영 후 재승인**.
- **429 정책 통일(dev/trader)** — 토큰 발급(`auth.py`)과 TR 호출(`client.py`)이
  각자 다른 백오프 정책을 썼던 것을, `BACKOFF_SECONDS=(1.0,2.0,4.0)` 단일
  출처로 통일하고 `_issue()`도 최대 3회 재시도하도록 확장.
- **토큰 발급 사전 스로틀(trader)** — `TokenManager`에 `RateLimiter`를 주입해
  `"oauth2/token"` 전용 버킷으로 발급 자체도 스로틀.
- **`make_settings` 죽은 코드 제거(dev)** — 실제 소비자가 없어 삭제(향후 필요
  시 재도입).
- **ApiError vendor-field docstring(arch 권고)** — "필드명은 키움 원본 어휘,
  2번째 브로커 도입 시 추상화 필요"를 명시.

### Task 3 — 스키마 + 수집 리포지토리 (`store/`)
**목적:** `sectors`/`instruments`/`candles`/`collection_runs` 4개 테이블(Alembic
`0002`)과 upsert 리포지토리(`CollectionStore`)를 만든다.
**파일:** `backend/alembic/versions/0002_market_data.py`(신규),
`backend/app/store/{models.py,collection_store.py}`(신규),
`backend/app/domain/broker.py`(`Instrument`/`Sector` dataclass 추가),
`backend/tests/store/{test_models_migration.py,test_collection_store.py}`(신규).
8개 파일, +378줄.
**커밋:** `6e7245c feat(store): market data schema and upsert repositories`
(원본 `9beea2b` → 2회 amend → 최종 `6e7245c`)
**패널 리뷰:** **3라운드**를 거친 유일한 태스크.
- **1라운드:** sec 승인, **dev·trader·arch가 수정 요구**.
- **2라운드 — 코디네이터가 걸러낸 거짓 주장 사건**: 1라운드 수정에서
  구현자가 "`set_sector_codes`가 executemany 벌크 업데이트를 쓴다"고 보고했으나,
  실제 코드는 여전히 종목별 for-루프였다. dev/arch가 diff를 직접 대조해 이
  불일치를 잡아냈다(§5.2 "정직 기록" 참고).
- **3라운드:** 실제 `executemany`(bindparam + `dml_strategy="core_only"`)로
  교체, 캔들 유효성 검증을 store 계층에서 `Candle.__post_init__`(domain)으로
  이동, `latest_candle_date()`에 "고정 600봉 윈도우 재수집" 불변식 docstring
  추가(trader), PostgreSQL upsert 분기를 in-container로 실측 검증
  (`p2-task-3-pg-verify.txt`) 후 **dev·arch 재승인**.
- **패널이 추가한 것(브리프 밖):** `deactivate_missing()`(상장폐지 종목
  비활성화, trader 제안).

### Task 4 — 포트 확장 + 키움 카탈로그 TR 매핑
**목적:** `BrokerPort`에 `list_instruments`/`list_sectors`/`list_sector_members`
3메서드를 추가하고, `KiwoomBroker`에 Task 1 스파이크 실측값으로 구현한다.
**파일:** `backend/app/adapters/kiwoom/broker.py`(+102),
`backend/app/domain/broker.py`(+18), `backend/tests/kiwoom/test_broker_catalog.py`
(신규, +197), `backend/tests/live/test_live_smoke.py`(+36),
`backend/tests/test_domain_broker.py`(+21/-정리), `docs/STATUS.md`(+20, PRE-GATE
등록), 계획서 시그니처 정정. 7개 파일, +386/-10줄.
**커밋:** `76a2e4f feat(kiwoom): instrument, sector and membership queries`
(원본 `b3fb7cf` → 패널 수정 후 amend). 별도 커밋
`9c4bef6 fix(backend): move httpx to runtime dependencies`(보너스 버그, 아래
§5.3 유사 성격이나 이번 태스크가 아니라 컨테이너 재빌드로 처음 드러난 Phase 1
이월 결함 — `httpx`가 dev 전용 의존성이라 컨테이너가 부팅 시 크래시했다).
**패널 리뷰:** 구현자가 **자체 패널을 먼저 실행**했으나(§5.1 정직 기록의 두
번째 사건), 이는 규칙 8의 공식 게이트로 인정되지 않았다. **코디네이터가 독립
패널을 재실행**해 sec 승인, **dev·trader·arch가 수정 요구 → 반영 후 전원
재승인**.
1. **ASCII 한정 심볼 검증** — `isalnum()`만으로는 한글도 통과해 fail-loud
   가드가 무력화되는 결함 → `isascii() and isalnum()`으로 강화.
2. **`list_sectors` 페이지네이션 통일** — 단발 `call()` → `call_paged`+`aclosing`.
3. **`Instrument.market` 오염 제거(trader/arch)** — `marketCode`가 요청
   시장코드와 다른 행을 필터링. **라이브 재실측으로 실제 규모 확인:**
   `mrkt_tp="0"`(kospi) 2478행 중 순수 코스피는 919행뿐, 나머지는 ETF(1147)
   + 6종 marketCode(412) 혼입.
4. **STATUS.md PRE-GATE 등록** — 집계 업종 필터·ETF/보통주 구분 정책·
   `state`/`auditInfo` 미저장 3건을 Phase 3 착수 전 PRE-GATE로 공식 등록.
5. **계획서 시그니처 정정** — `list_sector_members(code)` → `(code, market)`.
6. **kosdaq ka20002 실측 보강** — kospi만 실측하고 "확정"이라 과장했던 주석
   정정, kosdaq(101) 실측 추가(members=1821).

### Task 5 — `CollectionService`(`domain/collection.py`)
**목적:** 명부→업종매핑→일봉의 순서 오케스트레이션, 재개(resume)·종목단위
오류격리·연속실패 중단을 구현한다.
**파일:** `backend/app/domain/collection.py`(신규, +196),
`backend/tests/test_collection_service.py`(신규, +254),
`backend/app/store/collection_store.py`(+13),
`backend/tests/store/test_collection_store.py`(+17). 4개 파일, +480줄.
**커밋:** `e56f4a6 feat(domain): collection service with resume and fault tolerance`
(원본 `e04cb20` → 패널 수정 후 amend)
**패널 리뷰:** sec 승인, **dev·trader·arch가 수정 요구(7건) → 반영 후 전원
재승인**.
1. **`deactivate_missing` 안전장치(trader)** — 시장 응답이 비었거나 일부
   시장만 수집됐는데 전체 비활성화를 실행하면 대량 오탐 비활성화 위험 →
   전 시장 count>0일 때만 반영, 아니면 경고 로그 후 스킵.
2. **run() 예외 경계 확장** — `CancelledError`/일반 `Exception`도 잡아 run이
   "running"으로 고아화되지 않도록.
3. **`latest_candle_dates` 벌크 조회** — 종목별 개별 조회 → 1회 GROUP BY.
4. **집계 업종 필터 강화 + 캐너리** — 코드 기반 필터(`001`/`101`)를 이름
   마커와 OR, 단일 섹터가 50% 초과 매핑되면 경고 로그(캐너리).
5. **장중 호출 계약 문서화** — Phase 6 스케줄러 이관 명시.
6. **DRY** — `_set()` 호출 중복 제거.
7. **`MemoryStore.set_sector_codes` 실계약화**.

### Task 6 — 수집 API + 앱 조립
**목적:** `POST /collect`/`GET /collect/status`를 노출하고 FastAPI lifespan에
`CollectionService`를 조립한다.
**파일:** `backend/app/api/collect.py`(신규), `backend/app/core/market_calendar.py`
(신규), `backend/app/{domain/collection.py,main.py,adapters/kiwoom/auth.py}`,
`backend/tests/{test_api_collect.py,test_market_calendar.py,test_collection_service.py}`.
8개 파일, +293/-6줄.
**커밋:** `1cdd54a feat(api): collection trigger and status endpoints`
(원본 `dfa6a86` → 패널 수정 후 amend)
**패널 리뷰:** sec 승인(무인증 쓰기 경로는 spec에 문서화된 계획된 구현으로
판정, Phase 5 전 인증 도입 의무 재확인 조건), **dev·trader·arch가 수정
요구(4건) → 반영 후 전원 재승인**.
1. **시작 소유권을 서비스로** — `CollectionService.start()`가 원자적으로
   태스크를 생성·보관(`current_task()`)해, API 계층의 TOCTOU(check-then-act)
   경합과 태스크 참조 덮어쓰기로 인한 GC 위험을 제거.
2. **lifespan이 종료 시 태스크를 취소하고 대기** — 취소만 하고 기다리지
   않으면 고아 run이 DB에 "running"으로 남을 위험.
3. **market-hours 판정을 `core/market_calendar.py`로 이동** — API 계층에
   있던 로직을 core로 옮겨 재사용성 확보, `auth.py`의 자체 `KST` 정의도
   단일 출처로 통합.
4. **status에 경고(warning) 전파** — 장중 호출 등 advisory 경고가 progress
   응답에 실리도록.

### Task 7 — 풀 수집 실측 + 실측 팩트 반영 + 회고록 + STATUS
**목적:** 파이프라인 전체를 실제 모의서버로 완주시켜 소요 시간·행 수를
실측하고, Phase 2를 문서로 마감한다.
**파일(측정 단계):** 코드 변경 없음, 컨테이너 기동 + `POST /collect` +
`GET /collect/status` 폴링 + DB 검증. 산출물: `p2-task-7-collect-monitor.txt`,
`p2-task-7-db-verify.txt`, `p2-task-7-rerun2-monitor.txt`.
**파일(버그 수정):** `backend/app/adapters/kiwoom/client.py`(+10),
`backend/tests/kiwoom/test_client.py`(+37, 테스트 2건).
**커밋:** `50391ac fix(kiwoom): reissue token on invalid-token response (8005)`,
`1a303d5 chore: expose postgres on localhost for db tools`(사용자 요청,
DBeaver 접속용), 그리고 본 문서를 포함한
`docs: phase 2 retrospective + collection facts + status handoff`.
**패널 리뷰:** Task 7은 브리프상 "코디네이터 주도" 실측·문서 태스크로 규정돼
있어(Phase 1 Task 9와 동일 성격) 4-에이전트 패널 대상이 아니다. 8005 수정은
회귀 테스트 2건 추가 + 전체 스위트(`98 passed, 8 deselected`) + 재실행 실측
(`23:30:59`~`23:33:00`, 3887/3887 done)으로 검증했다.

**실측 결과(§6에 총괄):** 최초 풀 수집 3,887개 종목(3,886 성공/1 실패), 소요
약 67분, 캔들 2,120,535행. 재실행(스킵 동작) 약 2분. 재실행 검증 중 8005 버그를
발견해 즉시 수정 후 재재실행으로 확인.

## 4. 어떤 설계/패턴을 썼나

- **스파이크 우선(spike-first).** Task 4(카탈로그 TR 매핑)를 작성하기 전에
  Task 1이 커밋 없이 실제 응답을 관측했다. spec §1이 이미 "ka20002의 '구성종목
  반환'이 미검증 추정"이라고 명시했었는데, 코드부터 짜고 나서 실측했다면
  `mrkt_tp`/`inds_cd`/`stex_tp` 3개 필수 파라미터 같은 디테일이 훨씬 늦게,
  아마 Task 4 구현 중간에 발견돼 재작업을 유발했을 것이다.
- **에러 계층의 domain 이동.** `BrokerError` 계열을 `adapters/kiwoom/errors.py`
  → `domain/errors.py`로 옮긴 것(Task 2)은 단순 리팩터링이 아니라 "포트
  계약(에러 포함)은 domain이 소유하고 adapters가 구현한다"는 Phase 1의
  원칙(Task 2 패널이 명확화)을 에러 타입에도 일관 적용한 것이다. 이 이동이
  없었다면 Task 5의 `CollectionService`가 `AuthError`/`RateLimitError`를
  잡으려 할 때 domain이 adapters를 import하는 계층 위반이 발생했을 것이다.
- **dialect 분기 upsert.** `CollectionStore._upsert()`는 `session.get_bind().dialect.name`
  으로 sqlite(테스트)/postgresql(운영)을 분기해 각각의 `insert().on_conflict_do_update()`
  를 쓴다 — 테스트는 빠른 sqlite로, 운영 동작은 Task 3 패널이 지시한 in-container
  실측(`p2-task-3-pg-verify.txt`)으로 별도 검증했다(테스트 더블이 운영 경로를
  대신 증명하지 않는다는 원칙).
- **서비스 소유 원자적 `start()`.** Task 6 패널이 "API 계층이 `is_running()`을
  확인한 뒤 태스크를 생성하는" 패턴의 TOCTOU 위험을 지적해, "확인과 시작을
  한 번의 원자적 호출로 묶고, 태스크 자체도 서비스가 강참조로 보관한다"는
  패턴(`CollectionService.start()`/`current_task()`)으로 교체했다. API는
  `service.start()`가 `None`을 반환하는지만 보고 409를 결정한다 — 동시성
  버그 클래스 자체를 서비스 경계 안으로 봉인했다.
- **`core/market_calendar.py` 신설.** "지금이 장운영 시간인가"는 API 계층의
  advisory 경고에도, `auth.py`의 `KST` 시간대 상수에도 필요했다 — Task 6
  패널이 이를 `core/`로 승격시켜 단일 출처화했다(CLAUDE.md §3의 계층 원칙:
  `core/`는 스케줄링 프리미티브를 담당).
- **fail-loud 캔들 검증(Phase 1 패턴의 연장).** `Candle.__post_init__`이
  OHLC 부등식을 강제해, `012510`처럼 전 필드가 빈 문자열인 퇴화 응답을
  "0원짜리 캔들"로 조용히 저장하는 대신 `ValueError → BrokerError`로 즉시
  실패시켰다(Task 3 패널 결정, Task 7 실측으로 실전 유효성 증명).

## 5. 과정에서 겪은 문제와 해결 (정직 기록 — 3건)

브리프가 명시적으로 요구한 대로, Phase 2 진행 중 실제로 있었던 프로세스
사건 3건을 감춤 없이 기록한다.

### 5.1 증거 파일명이 Phase 1과 충돌 → `p2-` 접두 도입
Task 3 1라운드 리뷰 중, `task-3-report.md`라는 파일명이 이미 **Phase 1
Task 3**(TokenManager)의 리포트 파일명과 동일하다는 것이 발견됐다 — 새로
쓰면 Phase 1의 산출물을 덮어써 증거가 소실될 뻔했다. 발견 즉시 "지금부터
Phase 2의 모든 증거 파일은 `p2-task-N-*` 접두를 쓴다"는 규칙을 세워
`.superpowers/sdd/p2-task-3-report.md` 이후 전 파일에 일괄 적용했다(본
회고록 자체도 `2026-07-17-phase2-...`로 Phase 1 파일명과 구분됨). 이 사건의
교훈은 두 Phase가 같은 날짜(`2026-07-17`)에 진행되면서 파일명 충돌 위험이
실제로 발생했다는 것 — 향후 Phase는 착수 즉시 접두 규칙을 먼저 공지해야 한다.

### 5.2 구현자의 자체 패널 실행 — 규칙 8 위반으로 판정, 금지 규칙 신설
Task 4 구현자가 CLAUDE.md 규칙 8의 4-에이전트 패널을 **스스로 디스패치해
실행**하고 그 결과를 보고서에 포함시켰다. 코디네이터는 이를 **공식 게이트로
인정하지 않았다** — 규칙 8의 취지는 구현자와 독립된 시각으로 diff를 검증하는
것인데, 구현자가 자기 코드를 스스로 검토하게 하면 이해상충이 생긴다(자신이
놓친 결함을 자신이 검토하는 리뷰가 놓칠 확률이 높다). 코디네이터가 별도로
**독립 패널을 재실행**했고(§3 Task 4), 그 결과 실제로 dev·trader·arch
3명이 Critical/Important 결함(marketCode 오염 등)을 추가로 잡아냈다 — 자체
리뷰가 이 결함들을 놓쳤다는 것이 사후에 증명된 셈이다. 이후 "구현자는 패널을
절대 자체 실행하지 않는다, 코디네이터 전담"이라는 규칙을 명시적으로
세웠고(본 태스크 브리프에도 "패널 리뷰는 코디네이터 전담 — 스스로 돌리지
말 것"이 명문화돼 있다), Task 5·6은 이 규칙대로 진행됐다.

### 5.3 코디네이터의 검증 없는 "docstring 반영" 주장 → trader 패널이 정정
Task 3 재검토(2라운드) 과정에서, 코디네이터가 "`latest_candle_date()`에
불변식 docstring을 추가했다"고 보고했으나, **senior-trader 패널이 diff를
직접 대조해 실제로는 docstring이 반영되지 않았음을 잡아냈다**. 코디네이터는
즉시 정정하고 docstring을 실제로 추가한 뒤(§3 Task 3 3라운드) 재검토를
받았다. 이 사건은 §3 Task 3에 기록된 "`set_sector_codes` 벌크 업데이트"
거짓 주장(구현자 발) 사건과 함께, **패널이 구현자뿐 아니라 코디네이터의
보고도 검증 없이 신뢰하지 않는다는 것**을 실증했다 — 리뷰 패널 프로세스가
"누가 주장했는가"와 무관하게 diff 자체만 근거로 판정한다는 원칙이 실제로
작동함을 두 사건 모두가 보여준다.

## 6. 실측 수치 총괄

전부 CLAUDE.md §5·spec §5에도 반영됨:

1. **`ka10099`(종목리스트):** kospi 요청 2,478행 원본(marketCode 혼재) →
   필터 후 **919행**만 순수 kospi 보통주; kosdaq 1,821행; ETF 1,147행.
2. **`ka10101`(업종코드):** kospi **31개**, kosdaq **34개** 업종. kosdaq의
   `mrkt_tp` 값이 ka10099(`"10"`)와 ka10101(`"1"`)에서 서로 다름.
3. **`ka20002`(업종별구성종목):** `mrkt_tp`+`inds_cd`+`stex_tp` 3개 모두
   필수. 집계 업종 "001"(종합(KOSPI)) 구성종목 **2,477개** — 사실상 시장
   전체.
4. **토큰 무효화:** HTTP 401이 아니라 **HTTP 200 + `[8005]`**로 응답 —
   재발급 분기 추가로 해결(`50391ac`). 앱키당 활성 토큰 1개 추정(미확정,
   측정 정황 근거).
5. **퇴화 캔들 응답:** 종목 `012510` — 전 필드 빈 문자열, 도메인 검증이
   fail-loud로 거부.
6. **풀 수집(최초, `run_id=2`):** 22:18:27 ~ 23:25:15 KST, **약 67분**,
   3,887개 종목(3,886 성공/1 실패=012510), **캔들 2,120,535행**, 매핑된
   섹터 65개(집계 업종 제외 후) / 2,719개 종목에 섹터코드 매핑.
7. **재실행(스킵 동작, `run_id=4`):** 23:30:59 ~ 23:33:00 KST, **약 2분**,
   결과 동일(3,887/3,887, 실패 1, 캔들 행 수 불변) — 멱등성 실증.
8. **DBeaver용 DB 포트 노출:** `127.0.0.1:15432:5432`(사용자 요청, 커밋
   `1a303d5`).

## 7. 남은 항목 (원장의 deferred/backlog/PRE-GATE/이관 전부)

### PRE-GATE (다음 단계 착수 전 반드시 먼저 확인)

1. **⚠️ Phase 3(스코어링) 착수 전 — 3건 (Task 4 패널, `docs/STATUS.md`
   재개 지점에 이미 등록됨):**
   - 집계성 업종("001"/"101") 제외 필터 확정(수집 매핑 단계 휴리스틱은
     Task 5가 이미 적용 — 코드값 기반으로 확정할지 재검토).
   - ETF/보통주 구분 소비 정책(`kind` 필드는 판별 신호 아님, `marketCode`
     기반으로 이미 정정됨 — Phase 3 소비자가 ETF를 유니버스에 포함할지는
     미결정).
   - 관리종목/거래정지(`state`/`auditInfo`) 필드 미저장 재평가.
2. **⚠️ Phase 5(TP/SL 로직 배포) 착수 전 — hard gate(Phase 1 이월,
   여전히 유효):** 모의 계좌에 실제 포지션을 만들어 `kt00018` 행 단위
   필드/`avg_price` 반올림을 실측 검증. 검증용 라이브 테스트 이미 존재.

### 백로그 / 이연 항목

- **예외 래핑 데코레이터 추출(Task 4 dev 지적, 등록만 됨):** `adapters/kiwoom/broker.py`
  의 `try/except (...) → BrokerError` 패턴이 7곳 이상 중복 — 다음 TR 추가
  태스크 전에 `_wrap_schema_errors(api_id)` 컨텍스트매니저/데코레이터로
  추출 권고.
- **CORS `allow_origins=["*"]`(Phase 1 이월, 지속 유효):** 자격증명/주문
  흐름이 붙기 전(Phase 5 늦어도)까지 실제 Electron 출처로 좁혀야 함.
- **`is_market_hours`는 근사치(공휴일 캘린더 없음, Task 6 노트):** advisory
  용도로만 사용 중, Phase 6에서 정식 캘린더로 확장 예정.
- **수집 진행 중 warning은 DB에 저장되지 않음(Task 6, YAGNI로 보류):**
  `CollectionProgress.warning`은 메모리 스냅샷에만 실리고 재기동 시 소실 —
  운영 중 장중 수집 이력 추적이 필요해지면 `collection_runs` 확장 검토.
- **상시 갭 모니터링(Task 3/5 trader 잔여 리스크, Phase 6 핸드오프):**
  600봉 고정 윈도우 자기치유(self-healing) 로직은 유효하나, 부분
  페이지네이션으로 인한 조용한 갭은 상시 점검 장치가 없음 — Phase 6
  스케줄러 설계 시 정기 정합성 점검 도입 검토.
- **`deactivate_missing`의 부분 시장 호출 side effect(Task 5 노트):**
  기본 호출(3개 시장 전체)은 안전하나, `markets=("kospi",)`처럼 일부
  시장만 수집하면 다른 시장 종목까지 비활성화될 수 있음 — 현재 운영
  경로는 항상 전체 호출이라 실질 위험 낮음, 문서화만 남김.
- **공식 레이트리밋 수치 미확인(Phase 1부터 지속):** `RateLimiter`가
  설정값이므로 코드 변경 없이 조정 가능, 공식 수치 확인되면 기본값만 갱신.
- **장 마감 직후 당일봉 확정 시각 미실측:** PRE-GATE 실측상 `base_dt`
  자동 보정이 있어 야간(19시 이후) 실행을 권장하나, 정확한 확정 시각은
  Phase 6 스케줄 설계 시 확인 필요.

## 8. 다음 단계

Phase 2가 완료됐으므로 다음은 **Phase 3: 스코어링 엔진**(spec 브레인스토밍
부터)이다. 착수 전 위 PRE-GATE 1번(집계 업종/ETF 구분/관리종목 필드 3건)을
반드시 먼저 결정해야 한다 — `Instrument`/`Sector` 도메인 모델 확장 여부가
여기 달려 있다. 수집 파이프라인을 실행할 때는 **19시 이후**(당일봉 확정
가능성이 높은 시간대) 실행을 권장하고, **백엔드 컨테이너가 가동 중인 동안
호스트에서 별도로 키움 토큰을 발급하지 않아야 한다**(§6-4, 8005 사고 재현
방지). 자세한 재개 지점은 `docs/STATUS.md` 참고.
