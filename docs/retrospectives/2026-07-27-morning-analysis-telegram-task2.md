# 2026-07-27 아침 분석 Telegram Task 2 회고

## 요청

성공한 아침 AI 분석을 기존 Telegram sender가 안전하게 재시도할 수 있도록,
분석 SQL read model과 durable 자동 outbox를 추가한다. 분석·브로커·Telegram
API를 직접 호출하지 않고, 기존 SQLite/PostgreSQL 저장소 경계 안에서만
동작해야 한다.

## 기존 상태와 설계 판단

- Task 1의 `MorningAnalysisSummary`는 불변 DTO와 엄격한 verdict 상한·후보
  순위 검증을 이미 제공했지만, 실제 `analysis_runs`/`analysis_verdicts`를
  읽거나 outbox를 만드는 저장소 구현은 없었다.
- `AnalysisRunRow`에는 실행 환경 칼럼이 없으므로, `AnalysisSummaryRunStore`
  생성자에 주입한 `run_environment`만 결과 DTO와 idempotency key에 사용했다.
  SQL 조회는 환경으로 추측해 필터하지 않고 `succeeded`이며 `finished_at`이
  있는 분석만 대상으로 한다.
- KST 당일 판정은 `coarse_utc_bounds`로 SQL 후보를 줄인 뒤
  `within_kst_day`로 확정한다. coarse 범위는 ±1일 여유가 있으므로 정확 판정
  전 SQL `LIMIT`을 적용하면 전일 후보가 당일 run을 밀어낼 수 있다. 따라서
  실제 당일 요약을 만든 뒤 서비스가 요청한 `limit`을 적용한다.
- JSON은 string 배열만 신뢰한다. 깨진 reasons/risk_flags는 해당 필드를 빈
  tuple로 바꾸고 행 하나당 `corrupted_rows`를 한 번 올린다. row 값 자체가
  DTO 검증을 못 통과하면 제외하며, 후보 순위의 gap/중복·후보 상한처럼 run
  전체 관계가 깨진 경우에는 모든 최종 후보를 제외해 보수적으로 표시한다.
  20건 초과 verdict는 전체를 격리한다. 이렇게 하면 깨진 DB 행이 승인
  후보를 만들어 내거나 전체 알림 loop를 죽이지 않는다.
- outbox의 idempotency는 `analysis-summary:{environment}:{run_id}`를 사용한다.
  presenter 본문은 payload에 넣지 않고 run id·환경·score 기준일만
  allowlist로 보존한다. outbox와 모든 delivery part는 한 transaction에서
  만들며, 민감 digest 보존 정책에 따라 생성 시각부터 24시간 뒤 scrub 대상이
  된다.

## 변경 파일과 위치

- `backend/app/store/notification_store.py`
  - `AnalysisSummaryRunStore`와 JSON 손상 격리 helper를 추가했다.
  - `NotificationStore.generated_analysis_run_ids` 및
    `materialize_analysis_summary`를 추가했다.
  - `MaterializedDigest`를 일반화한 `MaterializedNotification`으로 바꾸고,
    기존 digest materialization 반환형도 함께 교체했다.
  - 새 계약의 `outbox_payload`/`delivery_bodies` 조회명을 추가하고, 기존
    호출 호환을 위해 `load_*` wrapper를 유지했다.
- `backend/tests/notifications/test_notification_store.py`
  - 실제 SQLite 모델로 KST 전일/당일, failed/unfinished/succeeded,
    생성 이력, 오래된 run 정렬과 limit을 검증했다.
  - malformed JSON, 불연속 pick rank, 20건 초과 verdict가 fail-closed 및
    `corrupted_rows` 관측으로 귀결되는 회귀를 추가했다.
  - outbox의 idempotency, allowlist payload, NORMAL priority, sensitive
    digest retention, 24시간 purge 시각, delivery parts 원자 생성을 검증했다.
- `backend/tests/notifications/test_digest.py`
  - digest materialization의 새 `MaterializedNotification` 반환형을
    검증하도록 기계적으로 갱신했다.

## TDD와 검증

1. RED:
   `cd backend && uv run pytest tests/notifications/test_notification_store.py tests/notifications/test_analysis_summary.py -q`
   → `ImportError: cannot import name 'AnalysisSummaryRunStore'` (collection
   error 1건). 구현 전 신규 저장소 경계가 없음을 확인했다.
2. 최소 GREEN:
   같은 명령 → `35 passed in 0.26s`.
3. 회귀 조사:
   coarse KST SQL prefilter 앞에 `LIMIT`을 두자 전일 run이 먼저 잘려
   당일 run이 `()`로 누락되는 실패를 재현했다. `coarse_utc_bounds`와
   기존 `_started_on` 패턴을 비교해, 정확 `within_kst_day` 판정 뒤의
   결과 상한으로 복구했다.
4. 최종 관련 검증:
   `cd backend && uv run pytest tests/notifications/test_notification_store.py tests/notifications/test_analysis_summary.py tests/notifications/test_digest.py -q`
   → `48 passed in 0.36s`.
5. notification 회귀:
   `cd backend && uv run pytest tests/notifications -q`
   → `264 passed in 1.80s`.
6. 문법·공백:
   `uv run python -m compileall -q app/store/notification_store.py tests/notifications`
   및 `git diff --check` 통과. 프로젝트 dev 의존성에는 ruff가 없어
   `uv run ruff check`은 실행 파일 부재로 수행하지 못했다.
7. Fix round 1 RED: 실제 SQLite Text-affinity 열에 raw `b"\xff"`를 적재한
   `test_read_model은_SQLite_Text의_invalid_utf8_JSON을_빈값으로격리한다`는
   `_json_string_tuple`의 `json.loads(b"\xff")`에서 `UnicodeDecodeError`로
   실패했다. 처음 `CAST(... AS TEXT)` fixture는 SQLite DB-API가 helper 전에
   디코드 실패하므로 raw byte 바인딩으로 바로잡았다.
8. Fix round 1 GREEN: `UnicodeDecodeError`를 JSON 손상으로 함께 격리한 뒤
   focused test `1 passed, 6 deselected`, 관련 48건과 notifications 264건을
   모두 통과했다.

## 자체 검토

- 성공이지만 미종료인 run, failed run, 전일 run은 자동 대상과 최신 조회에서
  모두 제외된다.
- `/analysis`의 최근성은 run ID가 아니라 `finished_at DESC, id DESC`다.
  늦게 끝난 먼저 시작한 분석을 이전 결과로 잘못 표시하지 않는다.
- 20건 초과 verdict는 symbol 선두 20개를 임의 채택하지 않고 전체를
  fail-closed 격리해 `corrupted_rows`에 원문 행 수를 남긴다.
- 이름은 `InstrumentRow` left lookup이라 누락돼도 심볼만 전달할 수 있다.
- 환경은 DB에 없는 칼럼을 가정하지 않으며, `generated_analysis_run_ids`는
  환경 포함 idempotency prefix만 파싱한다.
- 분석 summary payload에는 모델 출력·presenter 문자열·자격 증명·주문 원문이
  없다. 실제 API·브로커·주문·운영 DB는 호출하지 않았다.

## 리뷰

네 전문 관점의 읽기 전용 패널을 수행했다.

- 개발자 최초 리뷰의 Important 두 건(20행 이후 임의 누락, 최신 run ID 정렬)을
  수정했다. 재검토는 Critical/Important 0으로 승인했고, KST 정확 판정 전
  SQL limit을 둘 수 없어 coarse 후보를 전부 읽는 비용은 Minor로 남겼다.
- 트레이더 최초 리뷰의 최신 완료 시각 문제를 수정했다. 재검토는
  Critical/Important/Minor 0으로 승인했다.
- 아키텍처 재검토는 Critical/Important 0으로 승인했다. 환경 provenance와
  coarse 후보 N+1 읽기는 미래 구조 변경 시 다룰 Minor로 기록했다.
- 보안 재검토는 allowlist payload, 단일 transaction, 24시간 scrub, raw
  원문을 남기지 않는 손상 로그를 확인해 Critical/Important/Minor 0으로
  승인했다. 환경 provenance는 직접 Task 제약 안에서는 수정 요구가 아닌
  실전 전환 전 장기 안전 과제로 기록했다.

## 우려사항

- `analysis_runs`/`score_runs`에 환경 provenance가 없어, 이 Task가 요구한
  주입 환경 라벨과 idempotency는 동일 DB의 mock/real 분석 출처를 SQL로
  검증할 수 없다. 이번 direct brief는 해당 칼럼 부재를 전제로 주입 환경을
  결과와 idempotency에 쓰고 성공 분석만 읽도록 명시했으므로 migration과
  분석 write path 변경은 수행하지 않았다. 향후 분석 입력이 환경별로 갈리면
  migration·write path·read filter·기존 행 fail-closed를 한 작업으로
  추가해야 한다.

> **2026-07-27 Task 6 후속 결정:** 위 환경 provenance 장기 과제는 현재
> collection·score·analysis가 코드와 스키마상 환경 독립 **공유 시장분석**이라는
> 확인으로 폐기·대체됐다. `run_environment`은 Telegram/broker 운영 런타임 label과
> outbox namespace이며, mock↔real 전환에는 같은 analysis run도 환경별 outbox가
> 한 번씩 생길 수 있다. 본문은 이를 `알림 환경`으로 명시한다. 장래 분석 입력을
> 환경별로 분리하기로 제품 설계가 바뀌면 그때 migration·write/read filter를 별도
> 설계 태스크로 다시 검토한다.
- coarse KST prefilter는 정확 날짜 필터가 아니므로 많은 전일/익일 손상 run이
  존재하면 한 tick의 read 비용은 커질 수 있다. 단순 SQL `LIMIT`은 실제
  당일 run을 누락시키는 RED로 확인됐다. 날짜 정확성보다 우선할 수 없어,
  keyset scan cap과 continuation/관측은 다음 서비스 조립 Task에서 다룬다.
