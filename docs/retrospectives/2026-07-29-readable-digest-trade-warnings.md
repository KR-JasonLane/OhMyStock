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
