# Phase 6 회고록 — 스케줄러/오케스트레이터 (2026-07-23)

> 규칙 4에 따른 태스크별 회고. 스펙
> `docs/specs/2026-07-23-phase6-scheduler-design.md`(v2, 결정 #37~#40),
> 계획서 `docs/plans/2026-07-23-phase6-scheduler-plan.md`(v2, Task 1~8).
> 상태: **Task 1~6·7a·8 완결, 7b(실환경 하루 관찰) 진행 중** — 7b 완료 시
> §7을 갱신한다.

## 0. 무엇을 만들었나 (비전문가용 요약)

지금까지는 매일 사람이 손으로 "수집해", "분석해", "매매 시작해"를 눌러야
했다. Phase 6은 백엔드 안에 **스케줄러**를 넣어 거래일마다 저녁 7시 수집 →
스코어링 → 아침 8:20 AI 분석 → 9시 자동매매 시작을 사람 없이 돌게 했다
(결정 #39 완전 자동). 서버가 재부팅돼도 컨테이너가 스스로 살아나고(결정
#40), 낮에 재기동하면 "오늘 할 일 중 안 한 것"을 DB로 판별해 즉시 만회한다
(캐치업). 그 과정에서 Phase 5 트레이딩 엔진의 실결함 2건(아래 Task 1·2)도
찾아 고쳤다.

## 1. 진행 방식

스펙 브레인스토밍(사용자 결정 4건 — AskUserQuestion) → 스펙 v1 → 4패널
리뷰(Critical 2·Important 10) → v2 델타 4/4 승인 → 계획서 → 4패널 계획
리뷰(Critical 1·Important 8) → v2 승인 → 태스크별 구현+4패널(규칙 8).
모든 태스크가 델타 재검토까지 전원 승인 후 커밋됐다.

| 커밋 | 내용 |
|---|---|
| 233ce98 | 스펙 v2 (결정 #37~#40) |
| 3268744 | 계획서 v2 (Task 1~8) |
| ae92ef4 | Task 1 — 일일 한도 DB 시딩 (P5 정정 ①, 0010) |
| 7293bf0 | Task 2 — 진입 래치 3분기 (P5 정정 ②, DropKind) |
| 77c3f00 | Task 3 — 순수 타임라인 평가기 + ScheduleConfig |
| cabfad8 | Task 4 — scheduler_events(0011) + 소유 스토어 판정 헬퍼 |
| 55e655f | Task 5 — SchedulerService 틱 루프 |
| 21eba21 | Task 6 — /schedule API + lifespan 조립 + 통합 테스트 3분기 |
| c5d7e36 | Task 7a — compose restart 정책 + 운영 문서 |

테스트: 687(P5 종료 시) → **766 passed** (신규 ~79건).

## 2. 태스크별 상세

### Task 1 — 일일 한도 DB 시딩 (P5 정정 ①, ae92ef4)

- **문제**: OrderCaps(§8-1 일일 건수/금액 한도)가 run 단위 인메모리
  (`_on_accepted`에서 새 인스턴스)라, 같은 날 재기동하면 한도가 0으로
  리셋 — 스케줄러가 도입하는 "같은 날 자동 재기동"이 일일 한도를 실질
  무력화한다. 스펙 리뷰 단계에서 실코드 대조로 발견.
- **구현**: `TradingStore.daily_order_usage(day, run_environment)` —
  당일(KST) 주문 집계(매수·매도 무구분 — check() 의미론), 환경 조인
  필터(리플레이↔모의 오염 차단), 취소 감사행 제외(구조 판별).
  `TradingService._seed_daily_caps()` — `_run()` 진입 직후 to_thread
  시딩(베이스 계약 준수), `buy_blocked`는 공유 판정 헬퍼
  `OrderCaps.exceeds_daily`(strict >)로 명시 복원, 당일 매수 존재 시
  진입 배치 게이트(`_entries_done`).
- **패널이 잡은 실결함(트레이더 Critical)**: `record_fill`이 프로덕션
  미배선이라 시장가 주문(req_price=0 — 지정가 폴백 진입·손절·킬스위치
  청산 전부)의 금액이 시딩에서 항상 0 → 이 태스크가 막으려던 실패
  모드가 시장가에 한해 그대로 재현. **수정**: `OrderRequest.ref_price`
  (브로커 미전송 감사 메타데이터) 신설 → 시장가 생성 3지점 배선 →
  `trade_orders.est_krw` 컬럼(0010) 영속 → 집계는 `max(est, req, 체결합)`.
- 아키텍트: `(trade_run_id, created_at)` 인덱스 부재(insert-only 성장 시
  재기동마다 풀 스캔) → 0010에 동시 반영. 개발자: `_StoreLike` Protocol
  표류·판정식 리터럴 중복 → 갱신·헬퍼 추출.

### Task 2 — 진입 래치 3분기 (P5 정정 ②, 7293bf0)

- **문제**: `_entries_done` 래치가 `_enter_positions()` 호출 **전** 무조건
  세팅 — 09:05 첫 사이클에 분석이 아직 없으면 09:10 분석 완료에도 그날
  진입이 영구 스킵(조용한 기회 상실). 분석 창(~09:20)과 모순.
- **구현**: `_enter_positions`가 "판정 성립 여부"를 반환 — ① 분석
  부재/신호일 불일치 → 재시도, ② 신선한 픽 0 → 래치(정상 판정),
  ③ 전 후보 기술적 드롭(빈 quote — degenerate 전례) → 재시도, ④ 전략
  탈락·발주 진행 → 래치. `DropKind` enum(strategic/technical) 도입,
  사전 필터·쿨다운 드롭까지 전부 DroppedCandidate로 수렴(래치 판정이
  분류 필드만 읽음). `_warn_once`로 재시도 사이클 경고 중복 억제.
- 패널: 보안 Important(쿨다운 경고 dedup 누락) → 쿨다운을 pre_drops로
  수렴해 구조적으로 해소(보안이 "재시도 발생 조건 자체 제거 — 더 강한
  해소"로 평가). 수용 트레이드오프 2건 문서화: 부분 배치 크래시 잔여
  후보 스킵, 창 내 슬롯 재개방 미재평가.

### Task 3 — 순수 타임라인 평가기 (77c3f00)

- `domain/orchestration/` 신설: `ScheduleConfig`(스펙 §5-2 1:1 —
  `max_attempts` 없음: 포기는 창 종료뿐), `timeline.evaluate()`(잡별
  서브평가기 4 + 공통 헬퍼 3, I/O·시계 호출 없음). 창 판정 열림
  포함(>=)/닫힘 미포함(<), 스코어링은 기준일(R) 마감(자정·주말 통과),
  트레이딩 60초 백오프 무한 재시도, SKIP(시도 못 함)/GAVE_UP(실패
  잔존) 구분.
- 패널: 보안 Important — "Reason 고정 리터럴" 계약이 타입 힌트뿐 →
  `Decision.__post_init__` fail-loud enum 강제(무인증 노출 표면에 예외
  원문 유입 구조 차단, 오류 메시지도 타입명만). 개발자 Important —
  SCORE 백오프 경계 테스트 누락 → 4잡 대칭 완성. 트레이더 Minor —
  스코어링 무이력 시 분석 실패가 run 행 없이 반환돼 백오프 대신 틱
  간격 재시도(무해 — 문서화, Task 4 전제 금지).

### Task 4 — scheduler_events + 판정 헬퍼 (cabfad8)

- `kst_time.py`(KST 날짜 판정 단일 구현 — 거친 UTC 프리필터+파이썬 정확
  비교), 4개 소유 스토어에 `has_completed_run`/`last_failed_finished_at`
  공통 시그니처 헬퍼(트레이딩만 §4-d 3분기 + run_environment 필수 인자),
  `SchedulerStore`(Protocol 위임 합성 + `record_event` enum 이중 방어 +
  `recent_events`), 0011(scheduler_events — 폴리모픽 run_id, FK 미설정
  의도), `score_reference_for`(19:00 정각 R 전환 = 수집 창 열림과 원자적
  동시 — 동일 config 필드 공유).
- 계획 리뷰 개발자 Critical의 예방적 반영: 날짜 판정은 반드시 KST 변환
  후(08:20 KST 시작 런 = UTC 전날 — SQL DATE 비교는 매일 오분류 →
  유료 LLM 반복 재트리거). 패널: 보안 Important — 트레이딩 헬퍼
  `run_environment` 기본값 "mock" 제거(생략 가능 신호가 실전 경계를
  조용히 무너뜨림 — daily_order_usage 관례 통일). 개발자 Important —
  트레이딩 UTC 경계 테스트 + 0010/0011 실마이그레이션 왕복 테스트.

### Task 5 — SchedulerService 틱 루프 (55e655f)

- BackgroundRunService 비상속 상주 루프. 로컬 Protocol 주입만
  (`_RunnableService`/`_SchedulerStoreLike`). 틱: R 산정 → facts
  구성(to_thread) → 인프로세스 `is_running()` 덧입힘(DB 좀비 running 행
  무시 — 데드락 구조 차단) → evaluate → Decision별 실행(per-decision
  예외 격리). 이벤트·로그 통합 dedup(SKIP/GAVE_UP/START_REJECTED —
  (잡,액션,사유,날짜) 키, 자정 리셋), TRIGGER/RETRY는 매 실행 기록(감사
  이력). 재기동 예산 프로세스당 1회 → dead 영속(스케줄러 사망 ≠ 기동된
  감시 run 사망 — 독립 태스크). `snapshot()`에 `next_attempt_at` 힌트.
- 패널: 개발자 Important — 로그 dedup 누락(창 닫힌 뒤 30초마다 수천 줄)
  → `_record_once`로 통합. 아키텍트 Important — 스펙 §7 "예정 시각"
  재료 부재 → 힌트 신설. 보안 관측 — 테스트 플래키(8회 중 1회, sleep(0)
  고정 횟수 대기) → 실시간 폴링 `_until`로 해소. START_REJECTED 사유를
  `is_running()` 재확인으로 ALREADY_RUNNING/CONFLICT 구분.

### Task 6 — API + 조립 + 통합 테스트 (21eba21)

- `/schedule/status`(무인증 — 고정 리터럴만, 이벤트 상한 서버 상수 20)·
  `/schedule/pause`·`resume`(trade 토큰). main.py: 기동 게이트(replay
  무조건 > `SCHEDULER_ENABLED`), 셧다운 시 스케줄러 최우선 취소·await.
  conftest autouse `SCHEDULER_ENABLED=false`(테스트 부팅 차단 — 보안
  계획 리뷰) + lifespan 회귀 3종.
- **통합 테스트 3분기**(트레이더 계획 리뷰 Important — 격리 유닛과 가짜
  서비스 테스트 사이 공백 봉합): ① 같은 날 재기동 → 시딩 → 이중 진입
  0건, ② 킬스위치 날 비재기동, ③ 매수 0건 크래시 → 정상 신규 진입.
  이 테스트가 **naive/aware datetime 비교 TypeError를 실발견** →
  `kst_time.as_aware_utc`로 4개 스토어 반환 정규화(통합 테스트의 가치
  실증).
- 패널: 아키텍트 Important — 비활성 사유가 main.py 게이트와 API에 이중
  구현(드리프트) → 게이트 판정 시점에 `scheduler_disabled_reason` 기록,
  API는 조회만.

### Task 7a — compose restart + 운영 문서 (c5d7e36)

- db/backend `restart: unless-stopped`(replay 제외) + 재부팅 수렴 주석
  (depends_on 미재평가 → alembic 백오프 수렴 — 아키텍트 §10-2).
  `.env.example` SCHEDULER_ENABLED 블록, STATUS.md 운영 절차 5항.
- 패널: 트레이더 Important 2 — ① env는 기동 시 1회만 읽힘(lru_cache):
  변경 후 **재시작 필수**(파일만 고치면 조용히 이전 값 — 조용한 실패
  모드), ② 반일장 우회의 복귀 데드라인(다음 거래일 19:00 수집 창 이전)
  미고지 → 양쪽 문서에 명시.
- **배포**: 재빌드·재기동 완료(2026-07-23 17:23 KST) — 0011 적용,
  스케줄러 가동, `/schedule/status`가 collect `window_not_open`·
  `next_attempt_at 19:00` + 오늘 몫 3종 `completed` 정확 판정(캐치업
  판정의 실데이터 첫 실증).

### Task 8 — 리플레이 같은 날 재기동 검증 (장외, 2026-07-23 17:2x)

프로덕션 무접점 방식(호스트 8001 + 인라인 env — .env 무변경, 리플레이
서버는 자체 토큰이라 8005 무관)으로 실증. 앵커 2026-06-25T09:00,
speed=1.0, 시드 035760. 증거 `.superpowers/sdd/p6-task-8-replay-restart.txt`.

- **run 1**: .env 실서버 한도(단건 150만)로 진입 차단 — 리플레이 계좌
  1억의 슬롯 995만 > 한도. 캡 가드 실증(발주 전 차단 — DB 주문 무기록,
  포지션 ENTRY_FAILED). 리플레이용 한도(단건 2천만)로 재기동.
- **run 2**: 진입 성사(지정가 307주, est_krw 9,962,150) → **SIGKILL
  크래시**. (주의: `uv run` 래퍼에 kill -9 하면 uvicorn 자식이 생존 —
  실제 프로세스를 pkill해야 크래시가 성립. 운영 노트.)
- **run 3(재기동)**: `daily caps seeded from earlier runs today
  (1 orders counted)` — 건수 1·금액 9,962,150 정확 복원, **진입 배치
  스킵(buy 주문 총 1건 유지 — 이중 진입 0)**, reconcile 포지션 승계 →
  `liquidate_all` 킬스위치 청산. **청산 시장가의 est_krw=9,992,850
  기록 — Task 1 트레이더 Critical 수정(ref_price 경로)의 실동작 실증.**
- 부수 실증: 좀비 running 행(run 2)이 완료도 실패도 아닌 무해 상태로
  잔존(설계 문서화 그대로), 리플레이 프로필 스케줄러 미기동 +
  `reason=replay_profile`, 정상 셧다운(run 1 kill TERM)은
  stopped(kill_switch=0) 기록 — §4-d 분기 원천 데이터 확인.

## 3. 7b — 실환경 수용 검증 (진행 중)

- ✅ ① 재기동 후 스케줄러 기동·status 확인(§2 Task 7a 배포 항목).
- ✅ ② 저녁 19:00 수집 자동 트리거(19:00:28 첫 틱 —
  `p6-task-7b-collect-trigger.txt`) — 단, **아래 실결함 2건을 관찰이
  즉시 적발**(7b의 존재 이유 실증):
  - **결함 A — 수집 완료 리터럴 불일치**: CollectionService의 완료
    status는 P2 유래 `"done"`인데 Task 4 판정이 `"succeeded"`로 가정 →
    성공한 수집(3분 완주)을 3분마다 무한 재트리거(run 49개 누적). 테스트
    가 가짜 리터럴로 왕복해 못 잡은 클래스. **수정**: 판정을 `"done"`으로
    + 실서비스 리터럴 회귀 테스트.
  - **결함 B — 스코어링 창 vs 기준일 의미론 충돌(더 심각)**: 스펙 §4-b
    원안("수집 완료 직후 저녁 체인")이 `scoring_reference_date`("오늘
    **이전** 마지막 평일" — P3 자정 배치 설계)와 충돌 — D일 저녁 실행은
    reference=D-1로 기록돼 ① 몫 판정 영구 불일치 → 성공한 스코어링을
    30초마다 무한 재트리거(실측 2분에 4 run), ② 아침 분석 signal=D-1 →
    익일 진입 신선도 가드 전부 거부 = **완전 자동매매가 조용히 무력화**
    되는 방향. **수정**: 스코어링 창 시작을 D 다음 날 자정으로 정정
    (스펙 §4-b 정정 반영 — 자정 이후엔 기준일 정합). 저녁의 잡 스코어링
    run 4건(reference=수)은 무해(자정 run이 최신으로 대체).
  - 두 결함 모두 스펙·계획·구현·패널 4중 리뷰를 통과하고 **실환경 관찰
    에서만 드러났다** — 단위 테스트가 실서비스 리터럴/의미론 대신 가정값
    으로 왕복하면 이 클래스를 놓친다는 교훈(§4 프로세스 회고 반영).
- ⏳ ③ 익 거래일(금 07-24) 분석 08:20 → 트레이딩 09:00 자동 start.
- ⏳ ④ 분석 늦은 도착 실경로 1회 유도(리스크 낮은 시점).
- ⏳ ⑤ 서버 재부팅 1회 → 자동 복귀 + 캐치업.

완료 시 이 절과 STATUS.md를 갱신한다.

## 4. 프로세스 회고

- **스펙·계획 리뷰가 구현 전에 실결함을 3건 선제 차단**: OrderCaps
  리셋(스펙 단계 실코드 대조), stopped 이중 의미(§4-d), UTC/KST 날짜
  경계(계획 단계 — 구현되었다면 유료 LLM 반복 호출 사고).
- **패널 지적 합류 패턴 재현**: max_attempts 폐기(트레이더 Critical +
  Important + 아키텍트 Important 동시 해소), 쿨다운 pre_drops 수렴(보안
  Important + 트레이더 Minor + 개발자 재현 시나리오 동시 해소).
- **통합 테스트의 실효**: "배선까지 실물로" 요구(트레이더)가 naive/aware
  TypeError를 사전에 잡았고, Task 8 리플레이가 est_krw 경로·시딩·이중
  진입 방지를 end-to-end로 실증.
- **편집-리뷰 경합**: 리뷰 중 파일이 갱신되는 경합이 재발(트레이더 T5가
  명시 언급) — 델타 요청에 "현재 파일 기준" 명시로 관리(기존 관행).

## 5. 이월/백로그

- **7b 잔여**(§3) → 완료 후 본 문서 갱신.
- **실전 전환 게이트 추가**: 스케줄러 dead 능동 알림(트레이더 T5 —
  로그·status뿐인 현 상태로는 실전 금지), 전일 킬스위치 발동 시 익일
  자동 재개 정책 재확인(§10-6).
- **Phase 8(텔레그램) 연계**: scheduler_events·run 테이블이 알림 원천,
  트레이딩 gave_up=감시 공백 최우선 경보, pause/resume 원격 표면.
- 비차단 백로그: 잡별 일일 트리거 상한(짧은 시간 N회 초과 시 자동
  pause+경보 — 보안 7b: 완료-리터럴 불일치 클래스가 ANALYZE 잡에서
  재발하면 클라우드 LLM·뉴스 API 과금 폭증 방향), status 리터럴 공용
  enum 승격 검토(done vs succeeded 이질성의 구조적 해소),
  MARKET 주문 ref_price 생성자 강제(트레이더 T1 Minor),
  OrderRequest 검증 단위 테스트, 좀비 running 행 기동 시 정리 배치
  (트레이더 T4 Minor), scheduler_store N+1 GROUP BY 최적화,
  `_enter_positions` 판정부/집행부 분리(아키텍트 T2 관찰),
  Task 8 게이트 진입 시점 민감도(P5 이월 유지).
