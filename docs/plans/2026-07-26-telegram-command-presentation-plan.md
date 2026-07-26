# Telegram 명령 응답 가독성 개선 구현 계획

> **에이전트 작업 필수 스킬:** 태스크별 구현에는
> `superpowers:subagent-driven-development` 또는
> `superpowers:executing-plans`를 사용한다. 모든 단계는 아래 체크박스로
> 추적한다.

**목표:** Telegram의 모든 명령 응답을 한국어 핵심 요약형으로 통일하고,
계좌 요약에 검증된 예수금을 추가하되 검증되지 않은 총자산은 계산하지 않는다.

**아키텍처:** `OperationsControl`은 기존 브로커 조회에서 얻은 예수금과
주문가능금액을 모두 `AccountSummary`에 보존한다. 새
`TelegramCommandPresenter`는 외부 의존 없는 순수 프레젠터로 금액·KST
시각·상태·명령 결과를 렌더링하며, `CommandProcessor`는 실행 상태 전이와
프레젠터 호출만 소유한다.

**기술 스택:** Python 3.12, pytest/AnyIO, dataclass, zoneinfo, FastAPI
lifespan, Telegram Bot API plain text

## 전역 제약

- 키움 어댑터, TR 요청·응답, 인증, 주문, 페이지네이션은 변경하지 않는다.
- 실제 Telegram·키움 호출과 상태 변경 명령은 구현 검증에 포함하지 않는다.
- 총자산은 임의 합산하지 않고 `확인 불가`로 표시한다.
- Telegram Markdown/HTML parse mode를 도입하지 않는다.
- 기존 durable intent, confirmation, lease, 감사, 민감정보 TTL 계약을
  변경하지 않는다.
- 각 구현 태스크는 RED→GREEN→REFACTOR 순서로 진행하고 구현 직후
  `$ohmystock-review-panel`의 네 상시 리뷰를 통과한다.
- Critical/Important 발견은 수정하고 해당 관점 재검토를 통과하기 전 다음
  태스크로 이동하지 않는다.
- 커밋 전 전체 메시지와 포함 파일을 사용자에게 제시하고 명시적 승인을
  기다린다.

---

## 파일 구조

- 수정 `backend/app/core/operations_control.py`
  - 계좌 스냅샷에 예수금 보존
- 생성 `backend/app/domain/notifications/presentation.py`
  - 모든 Telegram 명령 응답의 순수 렌더링
- 수정 `backend/app/domain/notifications/commands.py`
  - 인라인 렌더링 제거와 프레젠터 위임
- 수정 `backend/tests/test_operations_control.py`
  - 예수금 성공·부분 실패 계약
- 생성 `backend/tests/notifications/test_presentation.py`
  - 금액·시각·상태·명령별 출력 계약
- 수정 `backend/tests/notifications/test_commands.py`
  - durable 명령과 프레젠터 연결 회귀
- 수정 `backend/tests/test_telegram_lifespan.py`
  - 실제 명령 응답 경로의 새 계좌 필드와 비밀 무로그 회귀
- 생성 `docs/retrospectives/2026-07-26-telegram-command-presentation.md`
  - 두 태스크의 요청, 기존 상태, 판단, 위치, 리뷰, 검증 기록
- 수정 `docs/STATUS.md`
  - Phase 8 실제 수용 결과와 다음 단계 기록

---

### Task 1: 계좌 스냅샷에 검증된 예수금 보존

**파일**

- 수정: `backend/app/core/operations_control.py`
- 수정: `backend/tests/test_operations_control.py`
- 생성: `docs/retrospectives/2026-07-26-telegram-command-presentation.md`

**인터페이스**

- 입력: `BrokerPort.get_deposit() -> Deposit(total: int, available: int)`
- 출력:

```python
@dataclass(frozen=True)
class AccountSummary:
    deposit: int | None
    available_deposit: int | None
    total_eval: int | None
    total_profit: int | None
    total_return_rate: None
    realized_pnl: int | None
    realized_pnl_confidence: str
    trading_day: date
    as_of: datetime
    source: str
    failed_fields: tuple[str, ...] = ()
```

- 실패 계약: deposit 조회 실패 시 `deposit`과 `available_deposit` 모두
  `None`, `failed_fields`에는 `"deposit"` 한 건

- [ ] **Step 1: 예수금 보존 RED 테스트 작성**

`backend/tests/test_operations_control.py`의 기존 계좌 테스트에 다음 핵심
단언을 추가한다.

```python
summary = await control.account_summary()
assert summary.deposit == 1_000_000
assert summary.available_deposit == 1_000_000
```

deposit 부분 실패용 broker를 만들어 다음 계약을 별도 테스트한다.

```python
control.broker.deposit_error = RuntimeError("down")
summary = await control.account_summary()
assert summary.deposit is None
assert summary.available_deposit is None
assert summary.total_eval == 1_200_000
assert summary.failed_fields == ("deposit",)
```

- [ ] **Step 2: RED 실패 확인**

실행:

```bash
cd backend
uv run pytest \
  tests/test_operations_control.py::test_account는_두소스를_병렬조회하고_총수익률을_만들지_않는다 \
  tests/test_operations_control.py::test_deposit실패에도_balance와실현손익을_유지한다 -q
```

예상: 첫 테스트는 `AccountSummary`에 `deposit`이 없어 실패하고, 두 번째
테스트도 동일한 누락 계약 때문에 실패한다.

- [ ] **Step 3: 최소 구현**

`AccountSummary`의 첫 필드에 `deposit: int | None`을 추가한다.
`_load_account()`에서는 positional 인자 혼동을 막기 위해 keyword 인자로
구성한다.

```python
summary = AccountSummary(
    deposit=(
        None if isinstance(deposit, BaseException) else deposit.total
    ),
    available_deposit=(
        None if isinstance(deposit, BaseException) else deposit.available
    ),
    total_eval=(
        None if isinstance(balance, BaseException) else balance.total_eval
    ),
    total_profit=(
        None if isinstance(balance, BaseException) else balance.total_profit
    ),
    total_return_rate=None,
    realized_pnl=realized,
    realized_pnl_confidence=confidence,
    trading_day=as_of.astimezone(self.calendar.KST).date(),
    as_of=as_of,
    source="broker+trade_store",
    failed_fields=tuple(failed),
)
```

테스트용 `Broker`는 `deposit_error`가 있으면 `get_deposit()`에서 이를
발생시키게 한다.

- [ ] **Step 4: GREEN과 관련 회귀 확인**

```bash
cd backend
uv run pytest tests/test_operations_control.py \
  tests/notifications/test_digest.py \
  tests/test_telegram_lifespan.py -q
```

예상: 전부 통과. 기존 `AccountSummary` positional 생성이 남아 있다면
테스트 실패로 찾아 모두 keyword 계약으로 정정한다.

- [ ] **Step 5: Task 1 구현 회고 기록**

회고 문서의 Task 1 절에 다음을 기록한다.

- 실제 `/account` 출력에서 발견한 의미 차이
- `Deposit.total`을 조회하고도 버리던 기존 위치
- 총자산을 계산하지 않은 안전 판단
- 변경 파일과 클래스·함수 위치
- RED/GREEN 명령과 결과

- [ ] **Step 6: Task 1 리뷰 패널**

같은 Task 1 diff를 `senior-developer`, `senior-trader`,
`architecture-expert`, `security-expert`에게 병렬 읽기 전용 검토로
전달한다. 키움 어댑터/TR을 건드리지 않았음을 확인한다. Critical/Important를
수정하고 해당 리뷰어 재검토를 통과한 뒤 결과를 회고에 기록한다.

- [ ] **Step 7: Task 1 커밋 승인 게이트**

예정 메시지와 정확한 파일 목록을 제시하고 사용자 승인을 기다린다. 승인
전에는 `git commit`을 실행하지 않는다.

---

### Task 2: 전용 프레젠터와 모든 명령 응답 통일

**파일**

- 생성: `backend/app/domain/notifications/presentation.py`
- 생성: `backend/tests/notifications/test_presentation.py`
- 수정: `backend/app/domain/notifications/commands.py`
- 수정: `backend/tests/notifications/test_commands.py`
- 수정: `backend/tests/test_telegram_lifespan.py`
- 수정: `docs/retrospectives/2026-07-26-telegram-command-presentation.md`
- 수정: `docs/STATUS.md`

**인터페이스**

```python
class TelegramCommandPresenter:
    def status(self, status: Any) -> str: ...
    def account(self, summary: Any) -> str: ...
    def positions(self, summary: Any) -> str: ...
    def help(self) -> str: ...
    def confirmation(self, kind: CommandKind, command: str) -> str: ...
    def control_result(
        self, kind: CommandKind, status: str, *, applied: bool = True
    ) -> str: ...
    def existing_result(self, kind: CommandKind, status: str) -> str: ...
```

내부 순수 헬퍼:

```python
def _won(value: Any) -> str: ...
def _kst(value: Any) -> str: ...
def _field(value: Any, name: str, default: Any = None) -> Any: ...
```

`CommandProcessor.__init__`은 선택적
`presenter: TelegramCommandPresenter | None = None`을 keyword-only 인자로
받고, 미주입 시 기본 프레젠터를 만든다. 기존 호출부 호환성을 유지한다.

- [ ] **Step 1: 공통 포맷 RED 테스트**

새 `test_presentation.py`에 표 기반 테스트를 작성한다.

```python
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1_250_000, "1,250,000원"),
        (-12_300, "-12,300원"),
        (0, "0원"),
        (None, "확인 불가"),
        (True, "확인 불가"),
        ("invalid", "확인 불가"),
    ],
)
def test_금액은_원단위로안전하게표시한다(value, expected):
    assert _won(value) == expected
```

UTC 문자열과 aware datetime이
`2026-07-26 00:06:00 KST`로 바뀌고 naive/invalid 입력은 `확인 불가`가
되는 테스트를 추가한다.

- [ ] **Step 2: 공통 포맷 RED 확인**

```bash
cd backend
uv run pytest tests/notifications/test_presentation.py -q
```

예상: `app.domain.notifications.presentation` 모듈이 없어 collection error.

- [ ] **Step 3: 공통 포맷 최소 구현**

`presentation.py`를 만들고 `_won`, `_kst`, `_field`만 구현한다.
`_kst`는 `datetime.fromisoformat()`과 `ZoneInfo("Asia/Seoul")`을 사용하고,
naive 또는 파싱 실패 입력은 원문 대신 `확인 불가`를 반환한다.

- [ ] **Step 4: `/status` RED 테스트**

정상 스냅샷에서 다음 핵심 문자열을 단언한다.

```python
text = presenter.status(normal_status)
assert text.startswith("✅ 시스템 정상")
assert "📅 자동 일정  운영 중" in text
assert "📈 자동매매  대기" in text
assert "📦 관리 포지션  0개" in text
assert "🤖 Telegram  정상" in text
assert "📨 대기 메시지  0건" in text
assert "enabled=" not in text
assert "None" not in text
assert "+00:00" not in text
```

poller dead, outbox dead, backoff, dead letter, kill switch를 각각 한 변수만
바꾼 parametrized 테스트로 만들고 `⚠️` 또는 `🚨` 및 해당 한국어 원인이
표시되는지 검증한다. 알 수 없는 trading status는 `확인 필요`로 표시한다.

- [ ] **Step 5: `/status` GREEN 구현**

정상/주의/장애 심각도를 먼저 계산하고 구성요소 요약을 만든다. 정상 화면에는
run ID와 내부 loop 플래그를 숨긴다. 활성 kill switch와 dead 상태는 절대
정상으로 축소하지 않는다.

- [ ] **Step 6: `/account`와 `/positions` RED 테스트**

계좌 전체 성공 테스트는 다음을 포함한다.

```python
assert "💰 계좌 요약" in text
assert "예수금" in text and "10,000,000원" in text
assert "주문 가능" in text and "9,979,053원" in text
assert "보유주식 평가" in text and "0원" in text
assert "총자산" in text and "확인 불가" in text
assert "오늘 실현손익" in text and "(추정)" in text
assert "broker+trade_store" not in text
```

각 `failed_fields` 값은 한국어 경고로만 표시되는지 테스트한다. 빈 포지션,
복수 포지션, 손상 행 1건을 별도 테스트하고 손상 원문은 응답에 포함되지
않음을 단언한다.

- [ ] **Step 7: `/account`와 `/positions` GREEN 구현**

설계 문서 §6.2~§6.3의 문구를 그대로 구현한다. 포지션은 종목코드, 한국어
상태, 수량, 평균 진입가만 표시한다. 종목명·현재가를 얻기 위한 브로커 호출은
추가하지 않는다.

- [ ] **Step 8: `/help`와 제어 응답 RED 테스트**

`help()`가 조회/제어/확인 필요 세 구역과 명령 설명을 포함하고 `/confirm`을
일반 명령 목록에 넣지 않는지 검증한다.

pause, stop, resume 확인 요청, liquidate_all 확인 요청·접수·완료,
적용 불가, 기존 terminal 결과를 parametrized 테스트로 작성한다. `/stop`
응답에는 `신규 진입만`, `기존 포지션 감시는 계속`이 유지되어야 한다.

- [ ] **Step 9: `/help`와 제어 응답 GREEN 구현**

프레젠터의 `help`, `confirmation`, `control_result`, `existing_result`를
구현한다. confirmation token을 프레젠터 내부에 저장하거나 로그하지 않고
호출 시 받은 전체 `/confirm ...` 문자열만 반환한다.

- [ ] **Step 10: `CommandProcessor` 연결 RED 테스트**

`test_commands.py`의 지원 명령 테스트에 각 결과가 새 프레젠터 헤더로
시작하는 단언을 추가한다. test double presenter를 주입해 status/account/
positions/help와 제어 결과가 프레젠터를 통과하는지도 검증한다.

`test_telegram_lifespan.py`의 `SensitiveControl.account_summary()`에
`deposit="ACCOUNT_AMOUNT_SHOULD_NEVER_LOG"`를 추가하고 새 응답 경로에서도
금액·토큰·confirmation이 로그에 없음을 유지한다.

- [ ] **Step 11: `CommandProcessor` 연결 GREEN 구현**

기존 `_render_status`, `_render_account`, `_render_positions`,
`_terminal_response`를 제거하고 프레젠터 호출로 대체한다. 명령별
`CommandResult.kind`, `outbox_sensitive`, `ephemeral`, terminal 상태는
변경하지 않는다.

- [ ] **Step 12: 관련 GREEN·회귀 확인**

```bash
cd backend
uv run pytest \
  tests/notifications/test_presentation.py \
  tests/notifications/test_commands.py \
  tests/test_operations_control.py \
  tests/test_telegram_lifespan.py \
  tests/test_telegram_service.py -q
```

예상: 전부 통과하며 외부 호출은 발생하지 않는다.

- [ ] **Step 13: Task 2 회고와 STATUS 갱신**

회고 Task 2 절에 프레젠터 경계, 명령별 출력, 오류 축소, RED/GREEN, 관련
회귀 결과를 기록한다. `docs/STATUS.md`에는 실제 Telegram 조회 수용 성공,
발견된 계좌 의미 차이, 구현 상태와 다음 단계인 배포 후 조회 명령 재수용을
기록한다.

- [ ] **Step 14: Task 2 리뷰 패널**

`senior-developer`, `senior-trader`, `architecture-expert`,
`security-expert`에게 Task 2 diff와 설계·계획을 전달한다. Critical/Important
발견을 수정하고 해당 관점 재검토를 통과한다. 키움 경계를 건드리지 않았음을
다시 확인하고 리뷰 결과를 회고에 기록한다.

- [ ] **Step 15: 전체 비라이브 검증**

`superpowers:verification-before-completion`을 적용한다.

```bash
cd backend
uv run pytest
uv run python -m compileall -q app tests
cd ..
git diff --check
docker compose config --no-interpolate --quiet
```

예상: live marker 테스트는 프로젝트 기본 addopts에 따라 deselected되고,
나머지는 전부 통과한다. 실제 Telegram·키움 호출과 운영 상태 변경은 하지
않는다.

- [ ] **Step 16: Task 2 커밋 승인 게이트**

전체 검증 결과, 리뷰 결과, 예정 커밋 메시지, 정확한 포함 파일을 사용자에게
제시한다. 명시적 승인 후에만 커밋한다.

---

## 배포 후 별도 수용

구현 커밋 이후 별도 사용자 승인을 받아 운영 DB 마이그레이션 없는 backend
이미지 재빌드·재기동을 수행한다. 실제 Telegram에서 조회 전용 명령만
실행한다.

```text
/status
/account
/positions
/help
```

수용 기준:

- 네 응답이 새 아이콘·한국어·KST 형식으로 보인다.
- `/account`에 예수금이 나오며 총자산은 `확인 불가`다.
- `/status` 정상 화면에 내부 `enabled=True`, `None`, UTC 원문이 없다.
- 상태 변경 명령은 이 수용 범위에서 실행하지 않는다.
