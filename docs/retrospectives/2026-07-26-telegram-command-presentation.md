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

## Task 2 — 전용 프레젠터와 모든 명령 응답 통일

### 사용자 요청과 기존 상태

실제 모의 Telegram 수용에서 명령 송수신과 단일 운영자 인증은 성공했지만,
`/status`는 `enabled=True`, `status=idle`, UTC ISO 시각과 내부 loop 상태를
그대로 노출했다. `/account`는 금액 의미를 구분하지 않았고, `/positions`와
제어 응답도 영문 상태·내부 필드명·문장 형식이 서로 달랐다.

사용자는 명령 실행·확인·복구 의미론을 바꾸지 않은 채 모든 명령 응답을
한국어 핵심 요약형으로 통일하고, 예상하지 못한 값이나 손상 원문은 응답
전체를 실패시키지 말고 `확인 불가` 또는 건수 경고로 축소하도록 요청했다.

### 설계 판단

`app/domain/notifications/presentation.py`에 외부 시스템과 영속성에 의존하지
않는 `TelegramCommandPresenter`를 만들었다. `CommandProcessor`는 durable
inbox, intent, confirmation, lease, 감사와 공용 operations control 호출만
소유하고, 문자열 생성은 주입 가능한 프레젠터에 위임한다. 미주입 호출부에는
기본 프레젠터를 생성해 기존 조립 계약을 유지했다.

표현 경계는 다음 원칙을 적용한다.

- 금액은 불리언을 제외한 정수만 쉼표와 `원`으로 표시한다.
- timezone-aware 시각만 `Asia/Seoul`로 바꿔 KST로 표시한다.
- 정상 `/status`는 내부 run ID와 loop 플래그를 숨기고, dead·backoff·
  dead letter·kill switch·거래 실패만 한국어 원인으로 승격한다.
- `/account`는 예수금, 주문 가능, 보유주식 평가, 평가손익, 오늘 실현손익을
  분리하고 검증되지 않은 총자산은 `확인 불가`로 고정한다.
- `/positions`는 종목코드, 한국어 상태, 수량, 평균 진입가만 표시한다.
  종목명·현재가를 얻기 위한 브로커 호출을 추가하지 않았고 손상 원문은
  건수로만 축소한다.
- `/help`는 조회·제어·확인 필요 구역을 나누며 `/confirm`을 일반 명령으로
  안내하지 않는다.
- pause, stop, resume, 관리 포지션 청산의 완료·접수·적용 불가·기존 결과를
  같은 심각도 헤더로 통일한다. `/stop`의 신규 진입만 중지, 기존 포지션
  감시 지속, 재기동·다음 거래일 복귀 가능성은 그대로 유지한다.
- 확인 토큰을 프레젠터에 보관하지 않고 호출 시 받은 전체
  `/confirm …` 문자열만 응답 마지막 줄에 넣는다.
- 관리 청산의 자유 형식 `warning`은 내부 진단에만 남긴다.
  `LiquidationReason`을 하위 호환 optional 필드로 추가하고 결과 발생
  지점에서 사유를 결정해, Telegram 경계는 enum allowlist만 표시한다.
  기존 `status`, `account_fully_empty`, `warning`과 주문 실행 순서는
  변경하지 않는다.

### 변경 파일과 정확한 위치

- `backend/app/domain/notifications/presentation.py`
  - `_field`, `_won`, `_kst` 공통 축소 규칙과
    `TelegramCommandPresenter.status/account/positions/help/confirmation/
    control_result/existing_result`를 추가했다.
- `backend/app/domain/notifications/commands.py`
  - `CommandProcessor.__init__`에 keyword-only 선택적 `presenter`를
    추가했다.
  - 확인 발급·소비, 조회 네 명령, pause/stop/resume/liquidate_all의
    성공·접수·실패·기존 terminal 응답을 모두 프레젠터 호출로 바꾸고
    `_render_status`, `_render_account`, `_render_positions`,
    `_terminal_response`를 제거했다.
  - 청산 응답은 `LiquidationReason`만 고정 표시 코드로 바꾸며 자유 형식
    `warning`이나 브로커·종목 진단 원문은 읽거나 로그하지 않는다.
- `backend/app/domain/trading/models.py`
  - `LiquidationReason` enum과 하위 호환 optional
    `LiquidationResult.reason`을 추가했다.
- `backend/app/domain/trading/service.py`
  - 관리 청산 결과 20개 발생 지점에서 no target, run 변경, 비활성,
    영속 실패, 장외, 대사 실패, 상태·수량 불일치, 기존 미체결, 거래정지,
    잔여 포지션, 완료와 장 마감 미종결 사유를 구조화했다.
- `backend/app/core/operations_control.py`
  - 공용 제어 경계의 4개 결과 발생 지점에 대상 없음, 거래 비활성,
    미관리 잔고 사유를 부여하고 하위 결과 사유를 보존했다.
- `backend/tests/notifications/test_presentation.py`
  - 금액·KST·필드 읽기, 정상/주의/장애 상태, 계좌 전체/부분 실패, 빈/복수/
    손상 포지션, help, 확인, 제어와 기존 결과 계약을 표 기반으로 고정했다.
  - 복합 타입 원문이 예외나 응답 유출로 이어지지 않는 회귀를 추가했다.
- `backend/tests/notifications/test_commands.py`
  - 모든 지원 명령의 새 헤더와 민감 outbox 계약을 확인하고, test double
    프레젠터로 조회·제어·전체 confirm 명령의 위임을 검증했다.
- `backend/tests/test_telegram_lifespan.py`
  - 실제 명령 응답 경로의 민감 계좌 fixture에 새 `deposit`을 추가했다.
    안내문 마지막 줄에서 confirmation을 추출해 bot token, 금액,
    confirmation이 로그에 남지 않는 계약을 유지했다.
  - 실제 projector dead 스냅샷이 `/status` 시스템 장애로 보이는 통합
    회귀를 추가했다.
- `backend/app/core/telegram_service.py`
  - supervisor의 일시 오류·rate limit과 sender outbox 재시도를
    `degraded/backoff_components/backoff_reason`의 canonical 안전 스냅샷으로
    노출했다. 성공한 supervisor tick은 과거 오류 상태를 지워 현재 backoff만
    나타낸다.
- `backend/tests/test_telegram_service.py`
  - sender의 일시 오류가 안전한 `backoff_reason`으로 나타나는 계약을
    추가했다.
- `backend/tests/trading/test_models.py`,
  `backend/tests/trading/test_service.py`,
  `backend/tests/test_operations_control.py`
  - optional reason 하위 호환과 각 청산 발생 지점의 정확한 구조화 사유,
    주문 의미론 불변을 고정했다.
- `docs/STATUS.md`
  - 실제 Telegram 조회 수용 성공, 계좌 필드 의미 차이, Task 2 구현 상태와
    배포 후 조회 전용 재수용 순서를 현재 재개 지점에 기록했다.

### RED/GREEN과 관련 회귀

- 공통 포맷 RED:
  `uv run pytest tests/notifications/test_presentation.py -q`는 새 모듈이
  없어 collection error 1건으로 실패했다. `_field`, `_won`, `_kst` 최소
  구현 후 `12 passed`였다.
- `/status` RED는 `TelegramCommandPresenter` 부재로 collection error가
  났고, 최소 구현 후 누적 `23 passed`였다.
- `/account`와 `/positions` RED는 메서드 부재로 `7 failed, 23 passed`,
  GREEN은 누적 `30 passed`였다.
- `/help`와 제어 응답 RED는 메서드 부재로 `16 failed, 30 passed`,
  GREEN은 누적 `46 passed`였다.
- `CommandProcessor` 연결 RED는 기존 인라인 문구와 미지원 `presenter`
  인자로 `17 failed, 10 passed`였다. 연결 후 프레젠터·명령·민감 로그
  묶음은 `73 passed`였다.
- 예상하지 못한 복합 상태·실패 항목이 `TypeError`를 만들고 거래
  `failed/needs_attention`이 시스템 정상으로 축소되는 추가 RED
  `5 failed, 46 passed`를 확인했다. 타입 축소와 심각도 승격 후 프레젠터
  단위 `51 passed`였다.
- 1차 리뷰 C/I를 재현한 추가 RED는 불완전 스냅샷, 비정상 컬렉션,
  실제 Telegram backoff, 정상 중지 상태, 킬스위치 모드, 청산 안전 사유,
  표시 예외와 falsey 주입, 위험 명령 위임, sender backoff 계약에서
  `34 failed, 81 passed`였다. 수정 후 같은 focused 묶음은
  `115 passed`였다.
- brief의 관련 회귀
  `uv run pytest tests/notifications/test_presentation.py
  tests/notifications/test_commands.py tests/test_operations_control.py
  tests/test_telegram_lifespan.py tests/test_telegram_service.py -q`는
  `157 passed`, 기존 FastAPI/TestClient deprecation warning 1건이었다.
- 재검토에서 발견된 outbox `pending/dead_letter` 누락·비정상과
  projector/maintenance 사각지대 RED는 `10 failed, 75 passed`였고,
  fail-closed 및 필수 loop 반영 후 `85 passed`였다.
- 구조화 청산 사유 RED는 네 대상 테스트 파일이 `LiquidationReason`
  부재로 collection error 4건을 냈다. enum, 24개 production 발생 지점,
  reason-only 표시 allowlist 구현 후 focused 묶음은 `201 passed`,
  확장 관련 회귀는 `237 passed`, 기존 warning 1건이었다.
- 주문 전 대사 실패와 접수 후 대사 실패를 분리하는 추가 RED는 기존
  enum 멤버 부재로 collection error 1건이었고, 분리 구현 후 focused
  `194 passed`, 최종 확장 관련 회귀 `241 passed`, 기존 warning 1건이었다.
- 실제 Telegram·키움·주문·운영 PostgreSQL은 호출하지 않았고 키움
  어댑터·TR·인증·페이지네이션·PRE-GATE 코드는 변경하지 않았다.

### 리뷰 전 검증 범위

Task 2의 리뷰 대상은 프레젠터, 명령 연결, Telegram canonical 상태,
구조화 청산 사유를 위한 trading model/service와 공용 operations control,
관련 테스트와 이 문서 및 STATUS 변경이다. Task 0과 Task 1 커밋은
보존했으며, Task 1의 `operations_control.py`에는 deposit 계약을 바꾸지
않고 구조화 청산 사유만 추가했다. 키움 경계, 운영 이벤트 알림 본문,
다이제스트 본문은 변경하지 않았다.

### 1차 리뷰 발견과 수정

키움 어댑터·TR 호출부를 건드리지 않아 조건부 `broker-api-expert`는
포함하지 않았다. 네 상시 리뷰어가 같은 Task 2 diff를 독립 검토했고
Critical은 없었다.

- 개발·보안 리뷰의 상태 fail-open 지적을 반영해 scheduler, Telegram,
  필수 loop, outbox의 알려진 정상값을 명시적으로 확인한다. 누락·비활성·
  알 수 없는 값은 최소 `⚠️ 시스템 주의`로 승격한다.
- 개발·아키텍처 리뷰의 비정상 컬렉션 지적을 반영해 `failed_fields`,
  `positions`, `corrupted_rows`를 list/tuple만 허용하고 그 밖의 타입은
  원문 없이 확인 경고로 축소한다.
- 개발·트레이딩 리뷰의 상태 어휘 지적을 반영해 실제
  `TradingProgress.stopping/stopped`를 각각 `중지 처리 중/중지됨`으로
  표시하고, `stop_new_entries/liquidate_all`을 신규 진입 중지와 관리
  포지션 청산으로 구분했다.
- 아키텍처·트레이딩 리뷰의 실제 backoff 지적을 반영해 허구의 fixture
  필드만 의존하지 않고 `TelegramService`가 supervisor와 outbox의 현재
  backoff 구성요소를 canonical 스냅샷으로 만든다. 프레젠터는 수신·전송
  지연을 각각 표시한다.
- 아키텍처 리뷰의 표시 예외가 intent를 `unknown`으로 오염시키는 지적을
  반영해 `CommandProcessor._present()`가 고정 fallback으로 축소한다.
  제어 효과와 terminal 상태는 성공으로 확정되며, 로그에는 메서드명과
  예외 타입만 남기고 표시 입력·계좌 금액·토큰은 넣지 않는다.
- 트레이딩 리뷰의 청산 사유 소실 지적을 반영해 자유 형식
  `LiquidationResult.warning`을 렌더링하거나 로그하지 않고
  장 마감, 거래정지, 기존 미체결, 상태 변경, 거래 비활성, 대사 실패,
  대상 없음, 미관리 잔고의 고정 allowlist 코드로 축소했다.
- 트레이딩 리뷰의 손익 범위 지적을 반영해 `오늘 실현손익`을
  `오늘 실현손익 (관리매매)`로 표시해 브로커 계좌 전체 손익과 구분했다.
- 보안 리뷰의 금액 무로그 fixture 지적을 반영해 서로 다른 실제 정수 금액이
  민감 outbox 본문까지 도달함을 먼저 확인하고, 포맷된 금액과 원 정수가
  `caplog`에는 없음을 검증한다.
- 개발 리뷰의 위험 경로 테스트 공백과 falsey 주입 지적을 반영해 resume와
  관리 청산의 확인 소비, invalid confirm, 기존 terminal, 표시 fallback,
  명시적 falsey presenter를 test double로 고정했다.
- 개발 재검토의 outbox 핵심 수치 fail-open 지적을 반영해 `pending`과
  `dead_letter`를 비음수 exact int로만 정상 인정하고 누락·문자열·음수는
  메시지 전달 상태 주의로 승격했다.
- 개발 재검토의 자유 문구 청산 사유 역분류 지적은 사용자의 trading 경계
  확장 승인 후 `LiquidationReason`으로 해소했다. 모든 production 결과
  발생 지점이 사유를 정하고, Telegram은 `warning`을 읽지 않는다.
- 아키텍처 재검토의 필수 loop 사각지대 지적을 반영해 projector와
  maintenance의 dead·backoff도 canonical snapshot과 프레젠터 심각도에
  포함했다.

### 구현 패널 재검토 결과(독립 gate 전)

- `architecture-expert`는 실제 projector dead
  `TelegramService.snapshot()`→프레젠터 회귀와 전체 필수 loop 집계를
  확인하고 Critical/Important/Minor 0건으로 승인했다.
- `senior-developer`는 outbox 핵심 수치 fail-closed와 24개 production
  청산 결과 발생 지점의 구조화 reason을 확인해 Critical/Important 0건으로
  승인했다. canonical backoff와 legacy 원천의 중복 판정, 문자열 기반
  presenter dispatch와 로그 필드 이름은 동작을 막지 않는 Minor 2건으로
  남겼다.
- `senior-trader`는 주문 전 대사 실패와 접수 후 대사 실패가 같은 문구로
  표시되면 중복 매도 위험이 있다고 Important로 지적했다.
  `PREFLIGHT_RECONCILIATION_FAILED`와
  `POST_ACCEPT_RECONCILIATION_FAILED`로 분리하고, 후자는 주문 여부 불명과
  재청산 금지를 안내하도록 수정했다. 재검토에서
  Critical/Important/Minor 0건으로 승인했다.
- `security-expert`는 raw warning 무로그 회귀가 기본 로그 수준만 검사한다고
  Important로 지적했다. DEBUG 수준에서 전체 warning, 고유 주문 표식,
  수량 표식을 각각 검사하도록 수정했고, 재검토에서
  Critical/Important/Minor 0건으로 승인했다.
- 이 시점 패널의 Critical/Important는 0이었다. 키움 어댑터·TR, 인증,
  페이지네이션, PRE-GATE를 변경하지 않아 `broker-api-expert`는
  호출하지 않았다.

### 독립 Task 2 gate 발견과 수정

코디네이터의 독립 gate는 구현 패널 승인 뒤 Important 4건과 Minor 2건을
추가로 발견했다. 여섯 건 모두 이번 Task에서 수정했다.

- 청산 `(status, reason)` 조합을 명시적 allowlist로 만들었다.
  `succeeded`는 `COMPLETED`, `NO_TARGETS`, `UNMANAGED_BALANCE`만 허용하고,
  accepted/in-progress/needs-attention도 대응 reason 조합만 허용한다.
  reason 누락, 새 status, 불일치 조합은 자유 형식 `warning`을 보지 않고
  `needs_attention`으로 fail-closed 한다.
- `TelegramService` canonical degradation에 dispatcher 제어 명령 지연,
  모든 양수 supervisor failure와 internal error, ephemeral confirmation
  sender의 rate-limit·temporary backoff를 포함했다. 프레젠터는 canonical
  `backoff_components`가 있으면 이를 단일 출처로 사용하고, 필드가 없는
  이전 snapshot만 별도 legacy 정규화 함수로 처리한다.
- `/status`의 `positions_count`, poll 최근 성공 시각, running/run ID와
  idle/run ID 조합을 검증해 손상값을 최소 시스템 주의로 승격한다.
  canonical 구성요소 자체가 복합 손상값이어도 원문 없이 계약 경고로
  축소한다.
- STATUS 상단은 현재 다음 작업을 Task 2 커밋 승인 → 별도 배포 승인 →
  조회 전용 재수용의 한 승인 연쇄로 정리했다. 과거 Phase 8
  `1051 passed`와 Task 10 후속 문구는 역사 기록으로 명확히 구분했다.
- presenter fallback은 문자열 `getattr` 대신 bound callable을 받고,
  안전 로그 필드를 `exception_type`으로 고쳤다.
- 알 수 없는 `failed_fields` 항목은 무시하지 않고 원문 없는 일반 계좌
  조회 실패 경고로 표시한다.

gate RED focused 결과는 `23 failed, 162 passed`, 기존 warning 1건이었다.
canonical 구성요소 복합 손상 추가 RED 1건도 예외 발생을 확인했다. 수정 후
focused는 `187 passed`, 확장 관련 회귀는 `270 passed`, 기존 warning
1건이다.

### 독립 gate 수정 후 영향 재검토와 추가 보강

독립 gate 여섯 건 수정 뒤 네 관점 재검토에서 청산 표시 분류가 durable
intent 상태 전이까지 지배하지 않는 Important가 발견됐다. 손상된
`succeeded` 결과가 화면에서는 확인 필요로 보이지만 DB와 감사에는
`succeeded`로 남아 재물질화 시 녹색 완료로 바뀔 수 있었고, 미지 status는
불필요한 accepted monitor를 시작했다.

- `_LiquidationDisposition`이 `presentation_status`, `terminal_status`,
  `monitor`를 함께 결정하도록 `(status, reason)` 허용표를 단일화했다.
  초기 control 결과, accepted monitor, 재기동 `reconcile_unknown`이 모두
  같은 분류기를 사용한다.
- 허용되지 않은 terminal 결과와 미지 status는 durable
  `needs_attention`으로 종결한다. 손상된 accepted/in-progress는 이미
  SELL side effect가 시작됐을 수 있으므로 사용자 응답은
  `needs_attention`으로 낮추되 lease와 read-only monitor를 유지하며
  절대 제어를 재실행하지 않는다.
- 최초 terminal, 재물질화, accepted monitor, unknown 복구 RED는
  `4 failed, 60 passed`였고 수정 후 command 전체 `64 passed`였다.
  손상 accepted의 즉시 응답 kind와 monitor 지속을 함께 고정한 추가 RED
  1건 뒤 command 전체는 `65 passed`였다.

아키텍처 재검토는 durable sender의 단일 in-memory delivery ID가 복수 retry와
프로세스 재생성에서 실제 DB backlog를 놓칠 수 있다고 Important로 지적했다.

- `NotificationStore.delivery_state_snapshot()`이 같은 단일 aggregate
  `SELECT`에서 delivery 상태 4종과 `status=pending`,
  `attempt_count>0` retry 수를 계산한다.
- 대표 원인은 raw `last_error_kind`, HTTP 진단, delivery/outbox ID, 본문,
  재시도 시각을 반환하지 않고 `rate_limited`, `send_deadline`,
  `temporary_error`의 고정 우선순위로만 축소한다.
- `OutboxSender`의 in-memory delivery ID/reason 슬롯은 제거했다. 정상·dead
  tick 모두 durable aggregate를 읽으므로 복수 retry와 sender 재생성 뒤에도
  상태가 보존되고, `retry_delivery()` version fence가 실패하면 DB에 없는
  가짜 backoff를 만들지 않는다.
- 복수 retry, sender 재생성, fence 실패, raw error 미노출 RED 3건은
  `3 failed`였고 수정 후 `3 passed`였다. store와 sender 회귀는
  `47 passed`, 프레젠터·명령·lifespan까지 포함한 관련 회귀는
  `304 passed`, 기존 warning 1건이었다.

최종 네 관점 재검토 결과는 Critical/Important/Minor 0건이다.
키움 어댑터·TR, 인증, 주문, 페이지네이션, PRE-GATE를 변경하지 않아
`broker-api-expert`는 포함하지 않았다.

### 독립 gate 최종 sender 최초 적재 방어

독립 gate의 마지막 검토는 sender 재생성 직후 DB aggregate를 아직 한 번도
읽지 않았는데도 cache의 초기 0건을 정상으로 표시하는 Important를
발견했다. 실제 DB에 pending, retry 또는 dead-letter가 있어도 ephemeral
confirmation이 계속 우선되면 durable tick이 건너뛰어져 이 false-green이
유지될 수 있었다.

- `OutboxSender`는 명시적 `initialized=False`로 시작한다. snapshot은
  동기 DB I/O 없이 cache와 이 bool만 반환한다.
- delivery aggregate는 계속 `asyncio.to_thread()`에서 읽으며, 조회가
  성공해 cache를 교체한 직후에만 `initialized=True`로 전이한다. 조회
  예외나 건너뛴 durable tick은 미초기화 상태를 유지한다.
- `TelegramService` canonical은 composite outbox의 `initialized`가
  정확히 `True`가 아니면 sender를 degraded 구성요소로 포함한다.
  프레젠터도 독립적으로 메시지 전달 상태 확인 필요를 표시한다.
- 실제 SQLite DB에 pending/retry/dead-letter가 각각 있는 재생성 sender,
  empty DB의 최초 tick 전 주의→tick 후 정상, ephemeral 우선 처리로
  durable tick을 건너뛴 실제 service snapshot→프레젠터 경로의 RED
  5건은 `5 failed`였고 수정 후 `5 passed`였다.
- 프레젠터·sender·lifespan 묶음은 `153 passed`, 최종 관련 회귀는
  `309 passed`, 기존 warning 1건이었다.

이 수정의 재검토에서는 `initialized=True` 이후에도 ephemeral 응답이 계속
우선되면 outbox aggregate가 갱신되지 않아 새 pending/retry/dead-letter를
놓칠 수 있다는 Important와, 미초기화인데도 placeholder pending 0건을
표시하는 Minor가 추가로 발견됐다.

- `CompositeSender`는 ephemeral 전송 성공 시 durable claim/send와 lease는
  계속 건너뛰되, `OutboxSender.refresh_snapshot()`의 읽기 전용 aggregate만
  같은 tick에 실행한다. DB I/O는 계속 `asyncio.to_thread()` 경계 안에
  있고 snapshot은 cache만 읽는다.
- empty 상태로 한 번 초기화한 뒤 새 pending/retry/dead-letter를 DB에 만든
  RED와 새 retry의 service canonical→프레젠터 RED, 미초기화 pending
  `확인 불가` RED는 `4 failed`였고 수정 후 `4 passed`였다.
- `refresh_snapshot()`이 선택 기능으로 다시 약화되지 않게
  `DurableSenderPort`가 run, refresh, lease release, snapshot 계약을
  명시한다. Composite 조립은 누락 구현을 즉시 거부하며 test double도 같은
  no-op refresh 계약을 구현한다. 계약 RED 1건은 `1 failed` 뒤
  GREEN이었다.
- 프레젠터·sender·lifespan 최종 묶음은 `158 passed`, 확장 관련 회귀는
  `314 passed`, 기존 warning 1건이었다.

최신 exact diff를 네 상시 리뷰어가 다시 독립 검토해
Critical/Important/Minor 0건으로 승인했다. 실제 외부 API, 주문, 운영 DB는
호출하지 않았다.

### 최종 broad review 보완

최종 broad review는 공용 제어와 Telegram 명령 경계 사이의 Important 1건,
ephemeral sender 상태와 문서 수치의 Minor 2건을 발견했다.

- `OperationsControl`은 하위 결과가 `status="succeeded"`라는 이유만으로
  미관리 잔고가 있으면 reason을 `UNMANAGED_BALANCE`로 덮어썼다. 이제
  `reason=COMPLETED`까지 함께 만족할 때만 승격하고, 누락 또는 불일치
  reason은 그대로 보존해 `CommandProcessor`의 allowlist가
  fail-closed 한다.
- 실제 `OperationsControl`과 SQLite command/inbox store를 사용한 확인형
  청산 E2E에서 reason 누락과 `MARKET_CLOSED` 불일치가 모두 잘못
  `succeeded`가 되는 RED `2 failed`를 확인했다. 수정 후 두 경우 모두
  durable `needs_attention`으로 종결되고, 동일 inbox 재물질화도 빨간 기존
  결과를 반환하며 하위 청산 호출은 1회뿐임을 `2 passed`로 고정했다.
- ephemeral 확인 응답 sender가 rate-limit 또는 temporary backoff 뒤
  인증 실패를 만나면 circuit만 dead로 만들고 이전
  `backoff_reason`, retry 시각, 실패 횟수를 남겼다. 두 전이의 RED
  `2 failed` 뒤 인증 실패 분기에서 세 상태를 함께 초기화해
  `2 passed`를 확인했다.
- production `LiquidationResult(` 발생 지점을 `rg`로 다시 계산한 결과는
  `domain/trading/service.py` 20개,
  `core/operations_control.py` 4개, 합계 24개다.
- 첫 broad 수정 뒤 변경된 여덟 테스트 파일의 영향 회귀는 `318 passed`,
  기존 warning 1건이었다.
- 개발 재검토가 찾은 공유 인증 circuit 전파 사각지대도 닫았다. ephemeral
  sender가 rate-limit 또는 temporary backoff 중 다른 sender의 인증 실패로
  공유 circuit dead를 관측하면 `_clear_backoff_state()`로 retry 시각,
  실패 횟수, backoff 사유를 함께 지운다. 재전송 없이 dead만 남는 두
  전이의 RED `2 failed` 뒤 직접 인증 실패 두 경로와 함께 `4 passed`였다.
- 아키텍처 재검토는 initial 청산만 미관리 잔고를 덧붙이고 accepted monitor
  및 재기동 `reconcile_unknown`은 완료 결과를 그대로 통과시키는
  Important를 발견했다. `OperationsControl._with_unmanaged_balance()`를
  initial과 reconcile 양쪽에서 사용하며, 오직
  `succeeded + COMPLETED`만 `UNMANAGED_BALANCE`로 승격한다. broker 잔고
  조회 실패는 terminal로 축소하지 않아 기존 read-only retry와 주문
  비재실행 계약을 유지한다.
- accepted monitor와 unknown 복구의 durable terminal 뒤 동일 confirm
  재물질화가 일반 초록 완료로 바뀌는 RED `2 failed`를 확인했다. 새
  스키마 없이 기존 bounded terminal 진단 슬롯에 유효한
  `LiquidationReason.value`만 저장하고 같은 allowlist로 표시를 복원한다.
  손상된 `(status, reason)` 조합은 reason을 저장하지 않고 generic
  `needs_attention`으로 유지한다. 관련 focused `5 passed`, 변경된 여덟
  테스트 파일의 이 시점 영향 회귀는 `323 passed`, 기존 warning 1건이었다.
- 후속 개발·보안 재검토는 `needs_attention + COMPLETED`처럼 status가
  우연히 terminal과 같아도 allowlist에 없는 enum 조합이 저장될 수 있고,
  기존 reason 없는 `succeeded` 행이 재물질화에서 녹색 완료로 보일 수
  있음을 발견했다. `_LiquidationDisposition.terminal_reason`이 allowlist
  hit인 terminal reason만 직접 운반하는 단일 출처가 되며, store 경계도
  `LiquidationReason` enum 객체만 받아 value를 저장한다. 누락·미지·불일치
  reason은 제어를 재실행하지 않고 kind와 표시 모두 generic
  `needs_attention`으로 축소한다.
- initial·accepted monitor·unknown 복구 손상 reason, legacy NULL 성공,
  store 임의 문자열의 RED는 `5 failed, 2 passed`였다. 수정 후 같은
  focused는 `7 passed`, 변경된 여덟 테스트 파일의 최종 영향 회귀는
  `328 passed`, 기존 warning 1건이었다.

### 최종 비라이브 검증

- `cd backend && uv run pytest`
  → `1240 passed, 11 deselected, 1 warning`.
- `cd backend && uv run python -m compileall -q app tests`
  → exit 0.
- `git diff --check`
  → exit 0.
- `docker compose config --no-interpolate --quiet`
  → exit 0.
- warning 1건은 기존 FastAPI/TestClient의
  `StarletteDeprecationWarning`이다.
- 기본 pytest 설정에 따라 live marker 11건은 제외됐다. 실제 Telegram,
  키움, 주문, 운영 PostgreSQL은 호출하지 않았고 커밋도 수행하지 않았다.

## 실제 배포와 조회 재수용

- Task 2는
  `424f9f6`(`feat(telegram): present readable command responses`)로
  커밋했다.
- 사용자의 별도 배포 승인 후 backend 이미지를 재빌드·재기동했다. Codex
  실행 계정에는 Docker 소켓 권한이 없어 사용자가
  `sudo docker compose up -d --build backend`를 직접 실행했다.
- 재기동 후 localhost `/health`는 `status=ok`, `db=ok`, `mode=mock`을
  반환했다. `/schedule/status`는 scheduler가
  `enabled=true`, `paused=false`, `dead=false`임을 반환했다.
- 사용자가 실제 Telegram에서 조회 전용 `/status`, `/account`,
  `/positions`, `/help`를 차례로 전송했고 네 응답 모두 새 가독성 형식으로
  수신됐다. 상태 변경 명령과 주문은 실행하지 않았다.
- `/status`는 시스템 정상, 자동 일정 운영 중, 자동매매 대기, 관리 포지션
  0개, Telegram 정상, 대기 메시지 0건과 KST 기준 시각을 표시했다.
- `/account`는 예수금 9,979,053원, 주문 가능 9,979,053원, 보유주식 평가
  0원, 총자산 `확인 불가`, 평가손익 0원, 오늘 실현손익(관리매매)
  0원(추정)을 표시했다. 실제 수용에서 발견한 계좌 필드 의미 차이가
  의도대로 분리됐고 검증되지 않은 총자산 합산은 없었다.
- `/positions`는 관리 포지션이 없음을 짧게 표시했고, `/help`는 조회·제어·
  확인 필요 명령을 세 구역으로 나눴다.
- 다음 운영 검증은 자동 모의운용을 방해하지 않는 시간에 수행할
  7b-⑤ 재부팅 캐치업 검증이다.
