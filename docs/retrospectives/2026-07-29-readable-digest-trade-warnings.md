# 읽기 쉬운 Telegram 다이제스트 거래 경고 — Task 1 회고

## 요청과 기존 상태

장 마감 Telegram 다이제스트에는 주문·포지션·손익 요약만 있어, 후보가
진입하지 않았거나 실행 중 주의가 생긴 원인을 운영자가 알 수 없었다. 기존
`Digest`는 version 1 payload와 보존 본문 재표시 계약을 이미 사용하고 있었고,
자유 형식 `trade_runs.warnings`를 다이제스트에 안전하게 실을 DTO·parser·표시
계약은 없었다.

## 설계 판단

- domain에 허용 code, 안전 종목코드, 유동성 정수 금액만 받는
  `DigestTradeNotice`를 추가했다. 지원하지 않는 code·자유 텍스트·손상된
  종목코드·금액은 생성 단계에서 거부한다.
- `DigestSection`은 최대 5개 tuple notice와 중복 제거 후 전체 수인
  `notice_count`를 함께 검증한다. bool은 count로 허용하지 않는다.
- 새 payload는 version 1을 유지하고 section의 선택 필드 `notices`와
  `notice_count`만 추가한다. 과거 v1 payload에 두 필드가 모두 없으면 빈
  경고로 해석하고, 하나만 있거나 notice object schema가 손상되면 fail-closed
  한다.
- presenter는 DB 경고 원문을 표시하지 않고 고정 한국어 문구만 사용한다.
  분석 지연은 pipeline의 성공 상태·안전한 기준일과
  `analysis_reference_expected is True`가 동시에 확인될 때만 `정상 복구`로
  표시한다. 유동성 금액은 `Decimal`로 억 원 단위를 반올림한다.
- `/digest`의 기존 delivery part 우선 재전송 계약은 변경하지 않았다.

## 변경 파일과 정확한 위치

- `backend/app/domain/notifications/digest.py`
  - 40–143행: 허용목록 `DigestTradeNotice`와 `DigestSection` 경고 불변조건
  - 335–473행: 자동매매 다음의 거래 경고 presenter와 고정 문구·Decimal 금액 표시
  - 606–679행: version 1 선택 필드 serializer와 구형/신형 retained payload parser
- `backend/tests/notifications/test_digest.py`
  - 191–245행: DTO 및 section 실패 경계
  - 480–605행: payload 호환·손상 거부·한국어 표시·코드별 고정 문구
- `backend/tests/notifications/test_commands.py`
  - 146–159행: 현재 v1 선택 필드를 가진 `/digest` retained payload fixture

## 테스트 우선 구현과 검증

먼저 DTO import가 없는 상태에서 새 검증 테스트를 추가해 collection RED를
확인했다. payload 선택 필드와 presenter는 이 계약을 만족하는 최소 구현으로
추가했다. 타입이 손상된 code·symbol은 `TypeError` 대신 `ValueError`로
fail-closed하도록 추가 RED/GREEN을 한 번 더 수행했다. 실행·감시 notice의
종목코드가 고정 문구에 섞이지 않는 것도 RED/GREEN으로 사양에 맞췄다.

- RED: `cd backend && uv run pytest tests/notifications/test_digest.py -q`
  - `DigestTradeNotice` import 부재 collection error 확인
  - 추가 안전 타입 경계 RED: 2 failed (`TypeError`가 아닌 `ValueError` 요구)
  - 고정 문구 RED: 2 failed (quote/order에 종목코드가 붙는 문제)
- GREEN: 동일 단위 테스트 `76 passed`
- 관련 회귀: `uv run pytest tests/notifications/test_digest.py tests/notifications/test_commands.py tests/notifications/test_service.py tests/notifications/test_notification_store.py -q` → `181 passed`
- `uv run python -m compileall -q app/domain/notifications/digest.py tests/notifications/test_digest.py tests/notifications/test_commands.py` 통과
- `git diff --check` 통과

## 자체 검토와 변경하지 않은 경계

- retained parser는 새 notice object에 정확히 네 key를 요구하고, 두 선택
  필드가 함께 있을 때만 읽는다. 따라서 원문 경고나 임의 추가 필드는 payload와
  본문으로 전파되지 않는다.
- 최대 5개 표시와 `notice_count - len(notices)` 초과 건수는 DTO의 불변조건으로
  음수가 될 수 없다.
- broker adapter, 키움 TR, 주문·거래 로직, DB schema와
  `trade_runs.warnings`는 변경하지 않았다. 실제 Telegram·키움 API·주문·운영
  DB도 호출하지 않았다.
- `ruff`는 backend 개발 의존성에 없어 실행하지 못했다. 프로젝트 설정에도
  lint 도구가 정의되어 있지 않으며, compileall과 관련 pytest로 문법·회귀를
  확인했다.

## Fix round 2 — 경고 구조·분석 복구 fail-closed 보강

- 거래 경고 symbol은 공용 `validate_symbol`의 정확히 6자리 ASCII 영숫자
  계약을 재사용한다. 공용 validator의 원문 포함 예외는 DTO 밖으로 전달하지
  않고 고정 오류로 축약한다.
- `notice_count == 0`과 빈 notices가 정확히 함께 성립하도록 하고, pipeline
  section에는 거래 notices/count를 둘 수 없도록 `Digest` 교차 불변조건을
  추가했다. retained payload도 같은 생성 경계를 통과한다.
- 분석 지연 복구는 단순 날짜 파싱으로 추정하지 않는다. Task 2의
  `DigestRunStorePort.pipeline_summary()` 구현이 거래 캘린더로
  `analysis_reference_expected: bool`을 생산하며, Task 1 presenter는 그 값이
  정확히 `True`인 경우에만 복구로 표시한다. Task 1만 적용되어 값이 없으면
  미복구로 fail-closed한다.
- `unknown`은 presenter에서 명시적으로 generic 문구에 매핑한다. 그 외
  허용목록과 renderer 분기가 불일치하면 `ValueError`로 fail-loud한다.

## 패널 리뷰 결과와 마감

Task 1 최초 패널은 다음 Important 세 건을 발견했다.

- 분석 성공과 날짜 파싱만으로는 분석 기준일 정합을 보장하지 못한다.
- 양수 `notice_count`와 빈 notices 조합이 허용된다.
- 거래 경고 symbol 계약이 프로젝트 공용 6자리 형식보다 넓다.

세 건은 Fix round 2에서 각각 명시적 `analysis_reference_expected is True`,
notice count/빈 notices 동치 불변조건과 pipeline 교차 불변조건, 공용
`validate_symbol` 재사용으로 수정했다. 수정 뒤 해당 관점 재검토에서 모두
승인됐다. Task 1 태스크 리뷰가 별도로 발견한 liquidity 금액 15자리 상한
Important도 Fix round 1에서 수정한 뒤 승인됐다.

남은 Minor와 후속 제안은 다음과 같다.

- `quote_unstable`·`order_attention`에 종목을 표시할 경우 다른 경고와 표현이
  중복될 가능성
- notice count와 retained parser 가용성에 대한 추가 상한
- catch-up 다이제스트 제목 표현 개선
- 주문 상태 세분화와 거래정지 경고 코드 추가

이 항목들은 현재 승인 사양 밖의 후속 설계 제안으로 남겼다. 브로커 adapter,
TR, 인증, 주문 경로는 변경하지 않았으므로 `broker-api-expert` 리뷰는 적용하지
않았다. 최종 관련 회귀는 `195 passed`, compileall과 staged/unstaged
`git diff --check`는 통과했다.

## Task 2 — DB 경고 정규화와 read model 연결

### 요청과 기존 상태

Task 1은 안전 DTO·payload·presenter 계약을 준비했지만, 영속된
`trade_runs.warnings`를 안전 notice로 바꾸거나 pipeline의 분석 기준일 정합을
생산하지 않았다. 따라서 다이제스트에는 거래 경고가 없었고, 분석 지연 뒤의
정상 복구도 경고 문자열만으로 판정할 위험이 남아 있었다.

### 설계 판단

- store 전용 순수 모듈은 사전 컴파일한 allowlist 정규식으로 알려진 경고만
  `DigestTradeNotice`로 만든다. 숫자·종목코드 파싱 실패와 미지 원문은 자유
  텍스트를 보존하지 않는 `unknown`으로 격리한다.
- collector는 줄 단위 발생 순서를 보존하고, 알려진 notice는 DTO 동등성으로,
  unknown은 원문 줄의 SHA-256 digest로 내부에서만 중복 제거한다. 표시 tuple은
  최대 5개지만 전체 고유 수는 unknown의 서로 다른 원문까지 포함한다.
- `DigestRunStore`는 같은 거래일·실행환경 run만 `started_at`, `id` 순으로
  정렬해 collector에 넘긴다. 다른 환경·날짜의 경고, DB 원문, 거래 로직과
  schema는 read model 밖으로 나가지 않는다.
- `analysis_reference_expected`는 한국 거래 캘린더로 구한 직전 거래일과 score
  기준일이 모두 존재하고 정확히 일치할 때만 참이다. 캘린더가 기준일을 구하지
  못하는 경우까지 거짓으로 처리해 `None == None` 복구 오판을 막았다.

### 변경 파일과 정확한 위치

- `backend/app/store/digest_trade_notices.py`
  - `normalize_trade_warning()`과 `collect_trade_notices()`가 allowlist,
    unknown 격리, 중복·상한을 담당한다.
- `backend/app/store/notification_store.py`
  - `DigestRunStore.pipeline_summary()`가 안전한
    `analysis_reference_expected`를, `trading_summary()`가 정렬된 warning
    collector 결과를 `DigestSection`에 넣는다.
  - `_previous_trading_day()`가 한국 거래 캘린더 기반 strict 이전 거래일을
    최대 31일 탐색하고 실패 시 `None`을 반환한다.
- `backend/tests/notifications/test_digest_trade_notices.py`
  - 합성 warning allowlist, 손상·unknown 비노출, 중복·상한·전체 수를 검증한다.
- `backend/tests/notifications/test_notification_store.py`
  - 정상·불일치·캘린더 손상 분석 기준일을 각각 검증한다.
- `backend/tests/notifications/test_digest.py`
  - 같은 날짜 mock run 두 개와 다른 환경·다른 날짜 run을 SQLite에 넣어 격리,
    순서, payload·본문 원문 비노출을 합성 E2E로 검증한다.

### 테스트 우선 구현과 검증

- RED: `cd backend && uv run pytest tests/notifications/test_digest_trade_notices.py -q`
  → `ModuleNotFoundError: app.store.digest_trade_notices`로 실패를 확인했다.
- RED: `uv run pytest tests/notifications/test_notification_store.py -q`
  → `analysis_reference_expected` key 부재 2건으로 실패를 확인했다.
- 추가 RED: 캘린더가 직전 거래일을 찾지 못하는 테스트는 `None == None` 때문에
  잘못 참이 되는 1건 실패를 재현했고, 명시적 non-`None` 검사로 보완했다.
- GREEN: 정규화·store 단위 `37 passed`; 다이제스트 read model 포함
  `127 passed`를 확인했다.
- 관련 합성 E2E/회귀: `uv run pytest tests/notifications/test_digest_trade_notices.py
  tests/notifications/test_digest.py tests/notifications/test_commands.py
  tests/notifications/test_service.py tests/notifications/test_notification_store.py
  tests/notifications/test_morning_analysis_telegram_e2e.py -q` → `219 passed`.
- 전체 비라이브: `uv run python -m compileall -q app tests` 통과,
  `uv run pytest -q -m "not live"` → `1469 passed, 11 deselected, 1 warning`
  (기존 Starlette deprecation warning) 통과.

### 자체 검토와 변경하지 않은 경계

- collector 반환값·payload·본문에는 DB 경고 원문이나 SHA-256 digest가 없다.
  SQLite 합성 E2E도 unknown 합성 문자열이 어느 출력에도 없음을 확인했다.
- warning은 DB에서 읽기만 하며 `trade_runs.warnings` 쓰기 경로, 주문·체결·
  포지션 계산, broker/TR·인증, migration과 schema를 변경하지 않았다.
- 실제 Telegram·키움 API·주문·분석 재실행·운영 DB는 호출하지 않았다.

## Task 2 패널 Fix round 1

### 발견과 수정

- unknown이 여섯 번째 이후 처음 나타나면 단순 앞 5개 slice에서 generic 경고가
  사라졌다. 전체 고유 count는 유지하면서 마지막 표시 notice를 `unknown`으로
  대체해, 미확인 상태가 있으면 최대 5개 안에 반드시 한 번 포함되도록 했다.
- 실제 producer의 안정 템플릿을 allowlist에 연결했다. 단건·일일 주문 상한은
  `capacity`, 미확정 진입·pending-exit 확인 실패·청산 발주 재시도 소진·체결
  감사 저장 실패·수동 대사 필요는 `order_attention`, 거래정지로 자동 청산이
  불가능한 경우는 `quote_unstable`로 축약한다. 기술 경고 aggregate는 이미
  존재하는 종목별 경고와 중복되므로 제외한다.
- 분석 mismatch는 실제 ISO 날짜 두 개만 캡처하고 `date.fromisoformat()`까지
  통과해야 한다. gap은 comma 정수와 부호 있는 소수 둘째 자리 퍼센트가 모두
  있는 실제 형식만 허용한다. 전용 상태 제외 문구도 exact fullmatch만 허용한다.
- collector는 warning item 256개, item당 16,384자, line당 1,024자, 전체
  512줄, 고유 notice 128개를 명시 상한으로 둔다. 비문자·초과 입력은 처리 중
  원문을 반환하거나 예외를 내지 않고 단일 `unknown`으로 조기 축약한다.
  unknown 지문은 strip한 line으로 만들며 함수 밖으로 반환하지 않는다.
- `quote_unstable`과 `order_attention`은 presenter가 symbol을 숨기는 계약에
  맞춰 collector에서도 code 단위로 합치고 payload의 불필요한 symbol을 버린다.
- core calendar는 정적 휴장일 표 등록 여부를 공개하는 읽기 전용 helper를
  추가했다. 기존 `is_trading_day()`의 미등록 연도 평일 fallback은 유지하지만,
  다이제스트의 strict 이전 거래일은 coverage가 없는 연도에서 `None`으로
  fail-closed한다. 최신 analysis 동률은 `(started_at, id)` 큰 행을 선택한다.

### 확인한 producer 위치

- `backend/app/domain/trading/service.py`
  - 1037행 기술 aggregate, 1081–1092행 단건·일일 cap
  - 1195–1209행 진입 체결 감사 실패·미확정 진입
  - 1278–1345행 수동 ownership·exit 대사 필요
  - 1403–1405행 장 마감 청산 미완료
  - 1554–1556행 청산 체결 감사 실패
  - 1652–1657행 진입 취소 뒤 ownership 수동 대사
- `backend/app/domain/trading/monitor.py`
  - 633–635행 청산 발주 재시도 소진
  - 667–669행 부분체결 감사 실패
  - 793–796행 pending-exit 확인 실패
  - 898–900행 장 마감 미체결 주문
  - 921–949행 quote 연속 실패·결측·거래정지

### RED/GREEN과 검증

- calendar coverage import 부재를 collection RED로 확인한 뒤 지원 연도 2026과
  미등록 연도 2099 공개 helper를 구현했다.
- 정규화·collector 보강 테스트는 최초 `22 failed, 24 passed`로 각 누락 분기를
  확인했고, 최소 구현 뒤 `46 passed`가 됐다.
- 미등록 연도 기준일과 동일 시각 analysis ID tie-break는 `2 failed,
  18 passed` RED 뒤 store 수정으로 calendar와 함께 `46 passed`가 됐다.
- Task 2 관련 회귀는 `248 passed`, core calendar 포함 회귀는 `274 passed`다.
- `uv run python -m compileall -q app tests`, staged·unstaged
  `git diff --check`가 모두 통과했다.
- 거래 로직과 `trade_runs.warnings` 쓰기 경로, DB schema는 변경하지 않았다.

## Task 2 패널 Fix round 2

### 거래정지 의미 충돌

Fix round 1은 monitor의 `trading halted (...) — auto-exit impossible`를
`quote_unstable`로 매핑했다. 그러나 producer는 단순 feed 불안정이 아니라 종목
상태 조회로 거래정지를 확인한 뒤 자동 청산 불가능을 알린다
(`backend/app/domain/trading/monitor.py:942`). 이를 “보유 포지션 시세 조회
불안정”으로 표시하면 확정 상태를 더 약한 장애로 오표시한다. 새
`trading_halted` code는 승인 사양 밖이므로 추가하지 않고, 해당 원문은
`unknown`으로 fail-closed해 `일부 거래 상태 확인 필요`로 보인다.

### 실제 order producer와 count 계약

- `service.py:1128`의 기록된 source order 없는 검증 진입,
  `service.py:1335`의 청산 주문 소멸 뒤 ownership 불명,
  `service.py:1343`의 zero balance이나 position snapshot 누락,
  `service.py:1403`의 장 마감 청산 미완료를 exact fullmatch
  `order_attention`으로 추가했다.
- `quote_unstable`·`order_attention`의 표시 tuple은 고정 문구 code당 한 건만
  유지한다. 다만 고유 판정 key는 원래 `DigestTradeNotice` 전체
  `(code, symbol, amounts)`를 사용하므로 서로 다른 두 종목 사건은
  `notice_count == 2`로 남고 presenter의 `외 N건` 계산에서 사라지지 않는다.
  unknown 필수 표시와 최대 5개 상한은 그대로 유지한다.

### strict 캘린더 연도 경계

`_previous_trading_day()`는 탐색을 시작하기 전에 `trading_day.year` coverage를
확인하고, 탐색 중 후보 날짜의 연도도 다시 확인한다. 따라서 미지원 2027년에서
지원 2026년 후보로 역행하거나, 지원 2026년에서 미지원 2025년 후보로 넘어가는
양방향 경계 모두 `None`이다. 기존 scheduler의 평일 fallback은 바꾸지 않았다.

### 정규식 정리와 검증

comma 정수 정규식은 `_COMMA_INT`, `_POSITIVE_COMMA_INT`로 명명해 liquidity와
gap의 중복을 줄였으며 허용 형식은 바꾸지 않았다.

- RED: 정규화·count·연도 경계 `8 failed, 65 passed`.
- GREEN: 같은 단위 범위 `73 passed`.
- Task 2 관련 회귀 `255 passed`, core calendar 포함 `281 passed`.
- compileall과 staged·unstaged `git diff --check` 통과.
- 거래 로직, warning 쓰기 경로, schema는 변경하지 않았다.

## Task 2 최종 패널 마감

- 패널 Fix round 1과 Fix round 2 반영을 마쳤다.
- `senior-developer`, `senior-trader`, `architecture-expert`, `security-expert` 네 관점의 최종 재검토가 모두 승인됐다.
- broad final review 결과는 Critical 없음, Important 없음, Minor 없음, `Ready: Yes`다.
- controller의 최신 전체 비라이브 검증은 `1506 passed, 11 deselected`이며, 기존 Starlette warning 1건만 남았다.
- 이번 마감에서는 코드 변경과 커밋을 수행하지 않았다.

후속 권고는 다음 작업으로 분리한다.

- bounded overflow 규모 필드
- catch-up `오늘`
- 거래정지 전용 코드
