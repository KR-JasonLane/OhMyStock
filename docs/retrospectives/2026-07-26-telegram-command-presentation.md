# Telegram 명령 응답 가독성 회고

## 문서 범위

이 문서는 Telegram 명령 응답 가독성 작업의 구현 단위별 판단과 검증을
기록한다. Task 0은 가독성 구현 전에 발견된 기준선 실패를 분리해 바로잡은
선행 작업이다. Task 1은 `AccountSummary`에 `Deposit.total` 예수금을
보존하는 작업, Task 2는 `TelegramCommandPresenter`로 모든 명령 응답을
통일하고 전체 회귀를 마감하는 작업을 각각 독립 구현 단위로 다룬다.

## Task 0 — 공유 인증 circuit 기준선 실패

### 사용자 요청과 기존 상태

사용자는 가독성 작업을 시작하기 전에 전체 기준선에서 지속적으로 실패하던
공유 인증 circuit 테스트를 새 기능과 분리해 먼저 수정하도록 승인했다.

기존 테스트는 `NotificationStore`에 2026-07-24로 고정된 fixture 시계를
주입했지만, 별도로 만든 `OutboxSender`에는 시계를 주입하지 않았다. sender는
실제 UTC 시각으로 delivery 나이를 계산했고, 24시간이 지난 delivery로 판정해
인증 실패를 발생시키는 Telegram 전송 전에 만료 처리했다. 따라서
`TelegramAuthenticationError` 분기가 실행되지 않았고, 주입된 동일
`TelegramCircuit` 인스턴스가 `running`에 머물렀다.

### 설계 판단

production의 `OutboxSender`는 인증 실패를 받으면 이미 주입된 circuit에
`mark_dead("authentication_failed")`를 호출하고 있었다. 결함은 이 계약이
아니라 테스트의 서로 다른 시간원 때문에 해당 분기에 도달하지 못한 데
있었다.

이에 production 코드는 수정하지 않았다. 테스트가 fixture의 `clock`을
별도 sender에도 주입하도록 해 store의 생성 시각과 sender의 만료 판정 시각을
일치시켰다. 이 선택은 인증 실패 공유 circuit 계약만 실제로 검증하며,
429·일시·영구 오류 분류와 durable outbox 상태 전이를 변경하지 않는다.

### 변경 파일과 정확한 위치

- `backend/tests/test_telegram_service.py:242`
  - fixture 반환값에서 고정 `clock`을 받도록 변경했다.
- `backend/tests/test_telegram_service.py:246`
  - 공유 circuit을 검증하는 별도 `OutboxSender`에 `now=clock`을 주입했다.
- `backend/app/core/telegram_service.py:875`
  - 변경하지 않았다. 서로 다른 시계로 인해 먼저 실행되던 기존 만료
    판정의 근거 위치다.
- `backend/app/core/telegram_service.py:904,909`
  - 변경하지 않았다. 인증 실패를 잡아 공유 circuit을
    `dead/authentication_failed`로 전이하는 기존 계약의 근거 위치다.

### RED/GREEN과 리뷰 결과

- RED:
  `uv run pytest tests/test_telegram_service.py::test_인증실패는_주입된공유회로를_dead로_전파한다 -q`
  결과 `1 failed`. 기대값은 `dead/authentication_failed`, 실제 값은
  `running/None`이었다.
- GREEN: 같은 명령 결과 `1 passed`.
- 관련 회귀:
  `uv run pytest tests/test_telegram_service.py tests/test_telegram_lifespan.py -q`
  결과 `35 passed`, 기존 FastAPI/TestClient deprecation warning 1건이었다.
- `senior-developer`, `senior-trader`, `architecture-expert`,
  `security-expert` 4인 패널은 같은 diff를 독립 검토해
  Critical/Important/Minor 0건으로 승인했다.
- 코디네이터의 독립 Task 0 게이트도 Critical/Important/Minor 0건으로
  통과했다.
- 실제 Telegram API, 키움, 주문, 운영 DB는 호출하지 않았으며 커밋도
  수행하지 않았다.

## Task 1 — 계좌 스냅샷에 검증된 예수금 보존

### 사용자 요청과 기존 상태

`/account` 응답의 실제 의미를 프레젠터 경계로 옮기기 전에, 같은 예수금
조회 결과에 서로 다른 두 금액이 있음을 확인했다. `Deposit.total`은 예수금,
`Deposit.available`은 주문가능금액이고, `Balance.total_eval`은 보유주식
평가액이다. 기존 `OperationsControl._load_account()`는 이미 병렬로 받은
`Deposit.total`을 버리고 `available`만 `AccountSummary`에 보존했다.

### 설계 판단

`AccountSummary.deposit`에 검증된 원천값만 추가했다. 예수금과 보유주식
평가액을 더해 총자산으로 만들지는 않았다. 결제 예정 금액·미수 등을 포함한
전체 자산 산식은 프로젝트 실측으로 확정되지 않았으므로, 거짓으로 정확해
보이는 합계보다 `total_return_rate=None`과 이후 프레젠터의 `확인 불가` 계약을
유지하는 편이 안전하다.

예수금 조회가 실패하면 한 TR의 두 값이 모두 불확실해지므로 `deposit`과
`available_deposit`을 함께 `None`으로 만들고, 기존 부분 실패 표기인
`failed_fields=("deposit",)` 하나만 남겼다. 잔고와 실현손익은 독립 소스라
정상값을 계속 보존한다.

### 변경 파일과 정확한 위치

- `backend/app/core/operations_control.py:18-29`
  - `AccountSummary` 첫 필드에 `deposit: int | None`을 추가했다.
- `backend/app/core/operations_control.py:127-168`
  - `_load_account()`가 `Deposit.total`과 `Deposit.available`을 함께
    보존하도록 했고, 필드 순서 변경에 취약하지 않게 keyword 인자로 스냅샷을
    구성했다.
- `backend/tests/test_operations_control.py:58-66, 104-135`
  - 성공 경로의 예수금 보존과 예수금 부분 실패 시 독립 잔고 보존 계약을
    각각 고정했다.

### RED/GREEN과 검증 결과

- RED:
  `cd backend && uv run pytest tests/test_operations_control.py::test_account는_두소스를_병렬조회하고_총수익률을_만들지_않는다 tests/test_operations_control.py::test_deposit실패에도_balance와실현손익을_유지한다 -q`
  결과 `2 failed`. 두 테스트 모두 의도대로 `AccountSummary.deposit`
  속성 부재(`AttributeError`)로 실패했다.
- GREEN: 같은 명령 결과 `2 passed`.
- 관련 회귀:
  `cd backend && uv run pytest tests/test_operations_control.py tests/notifications/test_digest.py tests/test_telegram_lifespan.py -q`
  결과 `38 passed`, 기존 FastAPI/TestClient deprecation warning 1건이었다.
- 키움 어댑터·TR, 인증, 주문, 실제 Telegram·키움·운영 DB는 건드리거나
  호출하지 않았고 커밋도 수행하지 않았다.

### 리뷰 결과

키움 어댑터·TR 호출부, 인증, 주문, 페이지네이션, PRE-GATE 실측 코드는
변경하지 않았으므로 `broker-api-expert`는 범위에 포함하지 않았다.

- 1차 독립 검토에서 `senior-developer`는 예수금 실패 계약이
  `total_eval`만 단언해 평가손익·실현손익 보존 회귀를 막지 못한다고
  Important로 지적했다. `senior-trader`, `architecture-expert`,
  `security-expert`도 같은 누락을 Minor로 지적했다.
- 이에 예수금 실패 테스트에 `total_profit == 20_000`,
  `realized_pnl == -12_000`, `realized_pnl_confidence == "estimated"`
  단언을 추가했고, 지정 관련 회귀를 다시 실행해 `38 passed`를 확인했다.
- `senior-developer`는 Important 수정 후 재검토에서 승인했다.
  `senior-trader`와 `security-expert`도 Minor 수정 후 재검토에서
  Critical/Important/Minor 0건으로 승인했다. `architecture-expert`는
  1차 검토에서 같은 누락을 Minor로 지적하면서 구현을 승인했고, 해당
  Minor는 위 테스트 보강에 함께 반영했다. architecture 재검토는 수행하지
  않았다.
- 최종 완료 조건은 네 상시 리뷰어의 승인, Critical/Important 0건, 그리고
  1차 Minor 지적의 수정 반영으로 충족했다. 모든 리뷰어가 수정 후
  Critical/Important/Minor 0건을 재확인한 것으로 기록하지 않는다. 실제
  외부 API나 주문은 리뷰 과정에서도 호출하지 않았다.

## 이후 구현 단위의 범위

Task 2는 외부 의존 없는 `TelegramCommandPresenter`를 도입해 조회·제어·도움말·
확인형 위험 명령을 포함한 모든 명령 응답을 한 표현 경계로 통일하고,
Task 0~2 전체 비라이브 회귀와 리뷰를 마감하는 범위다.
