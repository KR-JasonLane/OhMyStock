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

## 이후 구현 단위의 범위

Task 1은 브로커의 `Deposit.total`을 버리지 않고 `AccountSummary.deposit`에
보존하되, 검증되지 않은 총자산이나 총수익률을 새로 계산하지 않는 범위다.
Task 2는 외부 의존 없는 `TelegramCommandPresenter`를 도입해 조회·제어·도움말·
확인형 위험 명령을 포함한 모든 명령 응답을 한 표현 경계로 통일하고,
Task 0~2 전체 비라이브 회귀와 리뷰를 마감하는 범위다.
