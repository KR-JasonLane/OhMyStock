# Phase 6 스케줄러/오케스트레이터 구현 계획서

> **For agentic workers:** 이 계획서는 태스크 단위로 구현한다. 각 태스크 완료 후
> 4-에이전트 패널(개발자·트레이더·아키텍트·보안) 전원 통과를 확인하고 다음으로
> 넘어간다(규칙 8). 스텝은 체크박스(`- [ ]`)로 추적한다.

**Goal:** 데일리 타임라인(수집 19:00 → 스코어링 체인 → 분석 08:20 → 트레이딩
09:00 자동 start)을 백엔드 내장 asyncio 스케줄러로 자동화한다. 완료 판정은
전부 DB 기준이라 재기동/재부팅 후 첫 틱이 곧 캐치업이다(결정 #40). 트레이딩
잡은 창(09:00~15:20) 내 60초 백오프 무한 재시도 — 보유 포지션 감시 공백
최소화가 최우선.

**Architecture:** 판정은 순수 평가기(`timeline.py` — 잡별 서브평가기), 부수효과는
`SchedulerService`(틱 루프, Protocol 주입 서비스의 `start()` 직접 호출),
facts/이벤트는 `scheduler_store`(insert-only 0011 + 소유 스토어 판정 헬퍼 위임).
BackgroundRunService 비상속(상주 루프 — 모델이 다름). P5 정정 2건(§4-c 진입
래치, §5-1 일일 한도 시딩)을 스케줄러 활성화보다 **선행** 구현한다.

**Tech Stack:** Python 3.12, asyncio(기존), SQLAlchemy/Alembic(기존). **신규
런타임 의존성 0, 신규 시크릿 0.**

**Spec:** `docs/specs/2026-07-23-phase6-scheduler-design.md` v2 (수치·조건의 단일
출처 — 4패널 승인). 결정 #37~#40.

## Global Constraints

- 커밋 메시지 사전 확인 필수, AI 흔적 금지(규칙 7). 태스크별 4-에이전트 패널
  (규칙 8). API-shaped 아님 — broker-api-expert 비소집(스펙 §9).
- 테스트 출력 캡처: `> ../.superpowers/sdd/p6-task-N-*.txt 2>&1`.
- `domain/orchestration/`은 adapters/store를 임포트하지 않는다. timeline.py는
  I/O·await 금지(순수) — facts는 호출자가 값으로 변환해 주입.
- ScheduleConfig 수치 하드코딩 금지 — 스펙 §5-2와 1:1. env 노출은
  `SCHEDULER_ENABLED` 하나뿐(YAGNI).
- `scheduler_events.reason`은 **고정 리터럴 집합만**(스펙 §6) — 예외 원문·심볼·
  금액 금지(무인증 status 노출 경로). 위반은 보안 패널이 잡는다.
- 스케줄러 활성 릴리스 순서: **Task 1(한도 시딩)·Task 2(진입 래치)가 스케줄러
  조립(Task 6)보다 먼저 머지**(스펙 §5-1 릴리스 순서 — 시딩 없는 같은 날
  재기동은 일일 매수 한도의 실질 무력화).
- 새 로그는 결정 #36 형식: `scheduler decision job=<j> action=<a> reason=<r>`.
- 문서는 한국어.

## 착수 순서 제약 (스펙에서 강제됨)

1. **Task 1·2 (P5 정정)가 스케줄러 조립보다 선행.** 논리적으로는 상호
   독립이나 **같은 파일·같은 상태 변수(`_entries_done`/`_enter_positions`)를
   수정하므로 순차 머지**(개발자·아키텍트 공통) — 나중에 구현하는 쪽이 먼저
   머지된 `_entries_done` 상태 머신 전체를 재확인한다. 각각 TradingService
   수정이므로 즉시 기존 스위트(687)로 회귀 확인.
2. **순수(Task 3) → 저장(Task 4) → 서비스(Task 5) → API/조립(Task 6).**
   timeline.py의 Decision 계약이 4·5의 소비 계약을 고정하므로 3이 먼저.
3. **Task 7a(compose/문서)·7b(실환경 수용 검증)·Task 8(리플레이 검증)은
   Task 6 이후.** 7a는 즉시 리뷰·커밋 가능, 7b는 다중 세션 관찰(코드
   무변경), Task 8은 장외 실행 가능(리플레이).

## 파일 구조 (신규/수정 총괄)

```
backend/app/
  domain/trading/service.py    # 수정(Task 1·2): OrderCaps 시딩 + 진입 배치 게이트, _entries_done 래치 정정
  store/trading_store.py       # 수정(Task 1): 당일 주문 집계·당일 진입 주문 존재 질의
  domain/orchestration/
    __init__.py    # 신규(빈)
    config.py      # 신규: ScheduleConfig (스펙 §5-2 1:1) + __post_init__ 검증
    timeline.py    # 신규: TimelineFacts/Decision + 잡별 순수 서브평가기 + evaluate()
    service.py     # 신규: SchedulerService (틱 루프, Protocol 주입, pause/resume, 재기동 예산 1회)
  store/scheduler_store.py     # 신규: scheduler_events insert + TimelineFacts 구성(소유 스토어 헬퍼 위임)
  store/collection_store.py, scoring_store.py, analysis_store.py, trading_store.py
                   # 수정(Task 4): "오늘 몫 완료/실패" 판정 헬퍼 노출 (read-only)
  store/models.py  # 수정(Task 4): SchedulerEvent ORM
  api/schedule.py  # 신규: GET /schedule/status, POST /schedule/{pause,resume}
  core/config.py   # 수정(Task 6): scheduler_enabled (기본 true)
  main.py          # 수정(Task 6): SchedulerService 조립·기동 게이트(replay 미기동)·셧다운 순서(스케줄러 최우선 취소)
backend/alembic/versions/
  0011_scheduler_events.py     # 신규(Task 4 — 0010은 Task 1 est_krw+인덱스가 선점)
backend/tests/
  conftest.py      # 수정(Task 6): autouse SCHEDULER_ENABLED=false
  orchestration/   # 신규: test_timeline.py, test_scheduler_service.py, test_schedule_api.py
  trading/         # 수정: 시딩·래치 회귀
docker-compose.yml # 수정(Task 7a): db/backend restart: unless-stopped (replay 제외)
```

---

### Task 1: P5 정정 ① — 일일 한도 DB 시딩 + 진입 배치 게이트 (스펙 §5-1)

**목적:** OrderCaps가 run 단위 인메모리(`service.py:190`)라 같은 날 재기동 시
일일 한도가 리셋되는 결함 봉쇄. 스케줄러의 "같은 날 재기동" 정책의 전제 조건.

**Files:** Modify `domain/trading/service.py`, `store/trading_store.py`;
Test `tests/trading/`(신규 케이스).

- [ ] TradingStore에 당일(KST 날짜) 주문 집계 질의 추가: `(건수, 금액 합)` —
  **매수·매도 구분 없이 합산**(§5-1 — check()의 side 무관 누적 의미론과 일치).
  당일 진입(BUY, 진입 phase) 주문 존재 여부 질의 추가.
- [ ] `_on_accepted`는 빈 OrderCaps 생성만 유지(베이스 계약: 동기·무예외).
  **DB 시딩은 `_run()` 진입 직후(create_run 다음) `asyncio.to_thread`로**:
  `order_count`/`order_krw` 대입 + **`buy_blocked`를 config 임계값과 직접
  비교해 명시 세팅**(check() 부수효과 재트리거에 맡기지 않음).
- [ ] 진입 배치 게이트: 당일 진입 주문이 DB에 존재하면 `_entries_done=True`로
  시작(재기동 run의 이중 진입 방지 1차 게이트). 부분 배치 크래시 시 나머지
  후보가 스킵되는 한계는 §5-1대로 수용 — 코드 주석으로 명시.
- [ ] 회귀: 당일 주문 존재 시 카운터 복원/래치/게이트 3종 + 주문 없는 날
  기존 동작 불변 + 기존 스위트 전체 녹색.

**커밋(제안):** `fix(trading): seed daily order caps from DB on same-day restart (panel)`

---

### Task 2: P5 정정 ② — 진입 래치(`_entries_done`) 정정 (스펙 §4-c)

**목적:** 래치가 `_enter_positions()` 호출 **전** 세팅돼(`service.py:247`), 09:05에
분석이 아직 없으면 09:10 분석 완료에도 그날 진입이 영구 스킵되는 조용한 기회
상실 제거.

**Files:** Modify `domain/trading/service.py`(+ 필요시 `selection.py`의 드롭
사유 분류 소비); Test `tests/trading/`.

- [ ] 래치 세팅 조건 3분기(스펙 §4-c):
  ① **신선한 분석 부재** → 래치 미세팅, 진입 창 내 다음 사이클 재시도.
  ② **픽 존재 + 전 후보가 전략 규칙 탈락**(갭 가드·거래대금 등) → 정상 판정,
  1회 배치 후 래치(현행 유지).
  ③ **전 후보가 기술적 사유(시세/컨텍스트 부재 — 빈 quote 등)로만 드롭** →
  판정 미성립, 래치 미세팅 + 재시도.
- [ ] **분류 필드 보강은 필요 조건**(개발자 패널 — 현행 사유 체계는 세 갈래로
  흩어져 있어 문자열 prefix 매칭으로는 패치워크가 됨):
  (a) `_enter_positions` 사전 필터(`ctx/md is None`, `service.py:356-359`) —
  DroppedCandidate 미경유, warnings 문자열뿐 → **기술적(③, 래치 미세팅)**.
  (b) `select_entries` 내 "price missing"(`selection.py:130-134`) — 자유
  텍스트뿐 → **기술적(③)**. (c) `_all_dropped`(no free slots/available_krw
  0/slot budget 0) — 계좌·슬롯 전역 상태로 인한 조기 종료 → **정상 판정(②,
  래치 유지)**(재시도해도 같은 계좌 상태 — 전략 탈락과 동급. ⚠️ 알려진
  잔여 케이스(트레이더 델타 Minor): 진입 창 내 기보유 종목 청산으로 슬롯이
  다시 열리는 좁은 조합에서는 기회 상실 — 의도적 트레이드오프로 수용,
  구현 주석에 명시). 세 경로에
  구조화된 분류(예: DroppedCandidate.kind: strategic|technical, (a)도
  DroppedCandidate로 수렴)를 도입해 래치 판정이 분류 필드만 읽게 한다.
- [ ] ③ 재시도는 매 폴링 사이클 quote 재호출을 유발 — 진입 창 내 재시도
  자체 백오프(N사이클당 1회) 도입 여부를 구현 시 판단(트레이더 Minor —
  별도 레이트리밋 버킷이라 필수는 아님, 판단 근거를 주석으로).
- [ ] Task 1의 DB 게이트와 상호작용 확인: 재시도 사이클이 이미 발주된 주문과
  중복되지 않음(①③은 주문 0건 상태에서만 성립하므로 자연 배타 — 테스트로 고정).
- [ ] 회귀: 분석 늦은 도착 시나리오(첫 사이클 부재→재시도 성공), 전략 탈락
  래치 유지, 기술 드롭 재시도, 기존 스위트 녹색.

**커밋(제안):** `fix(trading): retry entry batch when analysis absent or quotes degenerate (panel)`

---

### Task 3: 순수 도메인 — ScheduleConfig + timeline 평가기 (스펙 §3·§4·§5)

**목적:** 창/선행/완료/재시도 판정 전부를 I/O 없는 순수 함수로 — exit_rules와
동일 원칙. 여기의 Decision 계약이 Task 4·5를 고정한다.

**Files:** New `domain/orchestration/{__init__,config,timeline}.py`;
Test `tests/orchestration/test_timeline.py`.

- [ ] `ScheduleConfig`: 스펙 §5-2 1:1(시각 6종 + tick_interval_s + 잡별
  retry_backoff_s 4종) + `__post_init__` 검증(창 순서·양수).
- [ ] `TimelineFacts`(frozen dataclass): 잡별 오늘 몫 상태 — 완료 여부(트레이딩은
  `succeeded | running | stopped&kill_switch` 판정 결과를 **불리언으로 받음** —
  질의 자체는 store 소유), 마지막 실패 종료 시각, 실행 중 여부, 엔진 조립
  여부, paused.
- [ ] `Decision(job, action, reason)` — action/reason은 스펙 §6 고정 리터럴
  enum. 잡별 순수 서브평가기(`_eval_collect/_eval_score/_eval_analyze/
  _eval_trade`) + `evaluate(now, facts, config)` 합성.
- [ ] 단위 테스트 전수(스펙 §9): 창 경계 — **열림 경계(19:00·08:20·09:00
  정각 ± 오프바이원)와 닫힘 경계(23:55·08:50·09:20·15:20)를 대칭으로
  전수**(트레이더 — 09:00은 완전 자동매매를 여는 가장 민감한 트리거),
  선행 미충족, 완료 멱등, 공휴일/주말 휴면, 자정 경계(스코어링 D몫), 잡별
  백오프(트레이딩 60초), 창 종료 포기, 킬스위치/셧다운/failed 3분기(불리언
  입력 계약 기준), 캐치업(낮 재부팅), paused 전 잡 휴면.

**커밋(제안):** `feat(sched): pure daily-timeline evaluator + schedule config (panel)`

---

### Task 4: 저장소 — 0011 + scheduler_store + 소유 스토어 판정 헬퍼 (스펙 §6)

**목적:** TimelineFacts의 DB 구성과 이벤트 적재(결정 #36 — SQL 복기 가능).

**Files:** New `store/scheduler_store.py`, `alembic/versions/0011_scheduler_events.py`;
Modify `store/models.py` + 4개 소유 스토어; Test `tests/`(store).

- [ ] `scheduler_events` 테이블(스펙 §6): id/ts/job/action/reason/run_id.
  insert-only. reason enum은 Task 3의 리터럴 재사용(문자열 저장).
  **run_id는 FK 미설정 — 의도적**(job 값에 따라 4개 run 테이블 중 하나를
  가리키는 폴리모픽 참조, 아키텍트 Minor — 단일 FK 시도 금지 주석).
- [ ] 소유 스토어에 판정 헬퍼 노출(아키텍트 — 원시 SQL 4벌 복제 금지).
  **공통 시그니처 통일**(개발자 — build_facts 합성이 분기투성이 되지 않게):
  `has_completed_run(reference_date: date) -> bool` /
  `last_failed_finished_at(reference_date: date) -> datetime | None`.
  - CollectionStore: 해당 날짜 시작 **status="done"** run 존재(⚠️ 수집만
    P2 유래 리터럴 — 정정 2026-07-23 7b: "succeeded" 가정이 실사고).
    AnalysisStore: 해당 날짜 시작 succeeded run 존재
  - ScoringStore: reference_date=D succeeded run 존재
  - TradingStore: **`succeeded OR (stopped AND stopped_by_kill_switch)` OR
    running** 판정(스펙 §4-d — 셧다운 stopped는 미완료; 시그니처는 동일)
- [ ] ⚠️ **날짜 판정은 반드시 KST 변환 후**(개발자 **Critical**):
  `started_at`은 UTC 저장이고 08:20 KST = **UTC 전날 23:20** — 변환 없이
  `DATE(started_at)` 비교하면 기본 설정의 정상 첫 분석 런조차 매일 "전날
  런"으로 오분류돼 창 내 ~8회 재트리거(유료 LLM·뉴스 API 낭비 + 감사
  오염). `started_at.astimezone(market_calendar.KST).date()` 관례
  (`trading/service.py`의 `_in_entry_window` 선례)를 store 질의에 적용.
- [ ] `SchedulerStore.build_facts(today)` — 위 헬퍼 합성 + `record_event()`.
- [ ] 테스트: 0011 왕복, 판정 질의 상태 조합(특히 stopped_by_kill_switch
  3분기, 자정 경계 날짜 산정), **08:20~09:00 KST 시작 런이 올바른 거래일로
  판정되는 UTC 경계 케이스(전 스토어)**, 이벤트 insert.

**커밋(제안):** `feat(sched): scheduler_events (0011) + per-store completion queries (panel)`

---

### Task 5: SchedulerService — 틱 루프 (스펙 §5·§8)

**목적:** 판정(Task 3)과 실행을 잇는 상주 루프. BackgroundRunService 비상속.

**Files:** New `domain/orchestration/service.py`;
Test `tests/orchestration/test_scheduler_service.py`.

- [ ] 생성자 주입: 서비스 4종(`start()/is_running()`만 갖는 최소 Protocol —
  `_StoreLike` 선례), **scheduler_store도 로컬 Protocol
  (`_SchedulerStoreLike` — `build_facts()`/`record_event()` 시그니처만)로
  타입**(아키텍트 — `app.store.*` 임포트 금지는 타입힌트에도 적용,
  `domain/trading/service.py:49` 선례 준용), config, 시계·sleep(테스트
  관례), 트레이딩 조립 여부.
- [ ] 틱 루프: facts 구성 → evaluate → Decision별 실행. per-decision
  try/except(1건 실패가 루프를 죽이지 않음 — ERROR 로그). `start()` None →
  `start_rejected` 이벤트 + 다음 틱 재평가. **실행 예외 이벤트도 고정
  리터럴만**(보안 Minor — 예: `execution_error`, 예외 원문은 ERROR 로그
  전용, 이벤트/status에 유입 금지).
- [ ] 이벤트 기록 + 결정 #36 로그 형식(INFO 트리거/WARNING 스킵·재시도/ERROR
  포기).
- [ ] pause/resume(인메모리) — paused는 facts에 반영돼 timeline이 휴면 판정.
- [ ] done 콜백: CRITICAL 로그 + **프로세스 수명당 총 1회** 재기동(스펙 §8),
  소진 후 dead 상태 노출(재기동해도 예산 리셋 없음 — 회복은 컨테이너 재시작).
- [ ] 테스트: 가짜 시계/서비스로 트리거·거부·이벤트·pause/resume·예외 생존·
  재기동 예산 소진 후 dead.

**커밋(제안):** `feat(sched): SchedulerService tick loop — protocol-injected, crash-budget 1 (panel)`

---

### Task 6: API + 앱 조립 + 테스트 게이트 (스펙 §5·§7·§8)

**목적:** lifespan 통합(기동 게이트·셧다운 순서)과 외부 표면.

**Files:** New `api/schedule.py`; Modify `core/config.py`, `main.py`,
`tests/conftest.py`; Test `tests/orchestration/test_schedule_api.py` +
lifespan 회귀.

- [ ] `Settings.scheduler_enabled`(기본 true). 기동 게이트: `run_environment
  == "replay"`면 미기동(무조건 — env보다 우선), `scheduler_enabled=false`면
  미기동. 미기동 사유는 기동 로그 1줄.
- [ ] main.py 조립: 서비스 4종 Protocol 배선(트레이딩 미조립이면 해당 잡만
  스킵 — facts의 엔진 조립 플래그), lifespan에서 `create_task`.
  **셧다운: 스케줄러 태스크 최우선 취소 + await 완료 확인 후** 기존 4-서비스
  정리 → broker.aclose() → engine.dispose()(스펙 §8 — 순서 명시 주석).
- [ ] `api/schedule.py`: GET status(무인증 — enabled/paused/dead + 잡별 오늘
  상태·예정 시각 + 최근 이벤트 ≤20, reason 리터럴만), POST pause/resume
  (`require_trade_token`).
- [ ] **conftest autouse fixture: `SCHEDULER_ENABLED=false` 기본 주입**(보안
  패널) + lifespan 회귀 테스트("기존 lifespan 테스트가 스케줄러를 기동하지
  않는다" + replay 게이트 + enabled 기동 경로 1종).
- [ ] **인증 계약 테스트(보안)**: `POST /schedule/pause`·`/resume` 각각
  X-API-Key 없음/오답 401 — 기존 `/trade/start,/stop` 개별 테스트 패턴
  동일(`test_api_security.py` 주석의 "라우터 배선 누락은 엔드포인트별
  실제 조립으로만 잡힌다" 전례).
- [ ] **reason 리터럴 계약 테스트(보안)**: `GET /schedule/status` 응답의
  모든 reason이 Task 3 리터럴 집합에 속함을 검증(특히 실행 예외 경로에서
  예외 문자열이 새지 않음).
- [ ] **같은 날 재기동 통합 테스트(트레이더 Important)**: 가짜 시계 + 얇은
  페이크 브로커를 두른 **실제 TradingService + 실제 SchedulerService**
  결합으로, 하루 안에 1차 진입 체결 후 강제 실패 → 스케줄러 Decision이
  재기동을 트리거 → 2차 run의 OrderCaps/`_entries_done`이 DB로 정확히
  시딩됨을 검증(§5-1이 막으려는 사고의 결합 경로 실증 — Task 1·2 격리
  테스트와 Task 5 가짜 서비스 테스트 사이의 공백 봉합).
- [ ] 전체 스위트 녹색(687+신규).

**커밋(제안):** `feat(sched): schedule api + lifespan wiring — replay/test gates, shutdown order (panel)`

---

### Task 7a: compose restart 정책 + 운영 문서 (스펙 §10-2·§10-5·§10-6, 결정 #40)

**목적:** 재부팅 무인 복귀의 코드/문서 변경 — 즉시 diff 리뷰·커밋 가능
(아키텍트 — 실시간 관찰이 필요한 7b와 분리해 패널 흐름 비차단).

**Files:** Modify `docker-compose.yml`, `.env.example`; STATUS.md 운영 절차.

- [ ] db/backend에 `restart: unless-stopped`(replay 프로필 제외). 재부팅 수렴
  메커니즘(backend 선기동 → alembic 실패 → Docker 백오프 수렴)을 compose
  주석 1줄로 명시(스펙 §10-2).
- [ ] `.env.example`에 SCHEDULER_ENABLED 항목 + 반일장 운영 절차 주석(§10-5 —
  알려진 시각 변경일은 `SCHEDULER_ENABLED=false` 원칙, pause는 단기 보조).
  시크릿 유입 없음(플래그 1개 — 보안 확인 완료).
- [ ] **STATUS.md 운영 절차에 §10-6 명시(보안 Important)**: 킬스위치
  stopped는 당일 스코프 — **다음 거래일 09:00 무통지 자동 재개**됨을 경고,
  지속 정지 절차(`SCHEDULER_ENABLED=false` 원칙, `/schedule/pause`는
  비영속 보조)와 함께 기록. 단일 인스턴스 전제(§10-4)도 운영 문서에 병기.
  추가 2건(T4·T5 패널 이월): ① "재시작=트레이딩 자동 재개 vs 킬스위치=
  수동 재개" 구분 명시(트레이더 T4 — 오인 방지), ② 실전 전환 게이트에
  "스케줄러 dead 상태 능동 알림(텔레그램/모니터링)" 항목 등재(트레이더
  T5 — dead는 로그+status 표시뿐이라 방치 위험).
- [ ] STATUS.md 갱신 후 전체 스위트 녹색 재확인(코드 변경은 compose뿐).

**커밋(제안):** `feat(ops): compose restart policy + scheduler ops notes (panel)`

---

### Task 7b: 실환경 수용 검증 (검증 전용 — 코드 무변경, 다중 세션)

**목적:** 실환경 하루 타임라인 완주 관찰. 코드 무변경이므로 패널 리뷰
비대상, 결과는 STATUS.md로 승계(세션 경계를 넘으면 핸드오프).

증거 `.superpowers/sdd/p6-task-7b-*.txt`.

- [ ] ① 컨테이너 재기동 → 스케줄러 기동·`/schedule/status` 확인(reason
  리터럴 노출도 육안 확인 — 트레이더 Minor).
- [ ] ② 저녁 19:00 수집 자동 트리거 → 스코어링 체인 관찰(이벤트·로그 grep —
  결정 #36 형식 실증).
- [ ] ③ 익 거래일 아침 분석 08:20 → 트레이딩 09:00 자동 start → 진입/감시
  관찰.
- [ ] ④ **분석 늦은 도착 실경로 1회 유도(트레이더 Important)**: 리스크 낮은
  시점에 분석 잡을 의도적으로 지연/일시 실패시켜 09:05 첫 사이클 픽 부재
  상태를 만들고, 창 내 재시도로 진입이 성사되는지 로그·DB로 확인(Task 2
  유닛 테스트가 못 보증하는 실 DB·타이밍 결합 검증).
- [ ] ⑤ 서버 재부팅 1회 → 자동 복귀 + 캐치업 실증.
- [ ] 결과를 STATUS.md + CLAUDE.md(해당 시 실측 노트)에 반영. 커밋은 문서만
  (해당 시 별도 컨펌).

---

### Task 8: 리플레이 검증 — 같은 날 재기동 시나리오 (스펙 §9 권장, 장외 가능)

**목적:** Task 1·2의 실동작(한도 시딩·이중 진입 방지·래치 재시도)을 리플레이
하네스로 실증 — R7 절차 재사용(speed=1.0).

- [ ] R7 기동 절차(STATUS.md 장중 실행 계획 ⓐ~ⓒ)로 리플레이 런: 진입 체결
  후 백엔드 강제 재기동 → 같은 날 재기동 run의 caps 시딩·진입 배치 스킵
  확인(감사 DB 질의). 분석 부재→재시도 경로는 리플레이 픽 주입 타이밍으로
  재현(가능 범위 확인 후 — 불가하면 단위 테스트 근거로 대체하고 명시).
- [ ] 증거 `.superpowers/sdd/p6-task-8-replay-*.txt`, `.env` override 원복.

**커밋(제안):** 검증 전용(코드 무변경 시 문서/STATUS만 — 해당 시 별도 컨펌).

---

## 계획 자체 점검 (self-review)

### 스펙 §X → Task 커버리지 매트릭스

| 스펙 | 내용 | Task |
|---|---|---|
| §3 | 계층 배치·순수 평가기·Protocol 주입·비상속 | 3·5 |
| §4 표+각주 | 잡 4종 창/선행/완료(킬스위치 분기 포함) | 3(판정)·4(질의) |
| §4-c | P5 정정: 진입 래치 3분기 | **2** |
| §4-d | stopped_by_kill_switch 완료 판정 | 4 |
| §5 | 틱 루프·재시도(창 내 백오프)·pause·기동 게이트 | 3·5·6 |
| §5-1 | P5 정정: 한도 시딩·진입 게이트·릴리스 순서 | **1** (순서 제약 Global) |
| §5-2 | ScheduleConfig 1:1 | 3 |
| §6 | 0011·reason 리터럴·소유 스토어 헬퍼·로그 형식 | 4·5 |
| §7 | API 3종·인증 스코프·노출 한정 | 6 |
| §8 | 예외 생존·재기동 예산 1회·셧다운 순서·stale_gate | 5·6 |
| §9 | 테스트 전략 전 항목(통합 테스트 포함) | 각 태스크 + 6·8 |
| §10-2 | restart 정책·재부팅 수렴 | 7a |
| §10-3 | pause 비영속 — status `paused` 노출 | 6 |
| §10-4 | 단일 인스턴스 전제 문서화 | 7a |
| §10-5 | 반일장 운영 절차(.env.example 주석) | 7a |
| §10-6 | 킬스위치 익일 재개 경고 + 지속 정지 절차(STATUS 운영 절차) | 7a |
| §11 | Phase 8/실전 게이트 연계 | (문서 — STATUS 이월 목록, 7a) |

### 점검 결과

- 스펙 전 조항이 태스크에 매핑됨. 코드 태스크 7(1~6, 7a) + 검증 2(7b·8).
- 계획 리뷰(4패널) 반영: 개발자 Critical(Task 4 UTC/KST 날짜 경계) +
  Important 8건(통합 테스트·7a/7b 분리·Protocol 타입·시그니처 통일·분류
  필드 필요 조건 격상·인증/리터럴 계약 테스트·§10-6 문서화·열림 경계
  테스트) 전부 체크리스트에 편입.
- 리스크 잔여: Task 7b는 실시간 관찰이라 세션 경계를 넘을 수 있음 —
  STATUS.md 핸드오프로 승계. Task 8의 "분석 부재→재시도" 리플레이 재현은
  가능 범위 확인 후 불가 시 7b-④(실환경 유도)가 대체 실증 경로.
