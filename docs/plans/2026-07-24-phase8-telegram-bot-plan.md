# Phase 8 텔레그램 봇 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** 4인 리뷰 패널 승인

**Goal:** 인증된 단일 운영자가 Telegram에서 OhMyStock의 긴급 알림과 장 마감 요약을 받고, 상태·계좌를 조회하며, durable하고 멱등적인 안전 제어를 실행할 수 있게 한다.

**Architecture:** FastAPI 프로세스에 TelegramService를 내장하되 polling, command worker, operational-event projector, sender를 독립 루프로 격리한다. REST와 Telegram은 공용 `OperationsControl`을 공유하며, 모든 수신·제어·발신은 PostgreSQL inbox/intent/outbox와 append-only operational event를 통해 재기동 후 복구한다.

**Tech Stack:** Python 3.12, FastAPI lifespan, asyncio, httpx, SQLAlchemy 2, Alembic, PostgreSQL, pytest, respx

## Global Constraints

- 기준 스펙은 `docs/specs/2026-07-24-phase8-telegram-bot-design.md`이며 충돌 시 스펙을 우선한다.
- 공식 Bot API를 `httpx`로 직접 호출하며 신규 런타임 의존성을 추가하지 않는다.
- Bot API origin은 `https://api.telegram.org` 상수이고 TLS 검증 활성, `follow_redirects=False`, `trust_env=False`를 고정한다.
- 초기 인증은 단일 `TELEGRAM_ALLOWED_USER_ID + TELEGRAM_ALLOWED_CHAT_ID + private chat` 정확 일치다.
- `/resume`은 scheduler의 인메모리 pause만 해제하며 trading 킬스위치를 되돌리지 않는다.
- `/liquidate_all`은 현재 run environment의 OhMyStock 관리 포지션만 대상으로 하며 계좌 전체 청산이 아니다.
- Telegram 장애와 DB 알림 장애는 trading/scheduler를 중단시키지 않는다.
- replay 프로필과 일반 테스트에서는 실제 Telegram·키움 API를 호출하지 않는다.
- 계좌 조회는 기존 `BrokerPort`, `TokenManager`, rate limiter를 공유하고 별도 키움 토큰을 발급하지 않는다.
- 문서·로그·감사에 bot token, 확인 token 원문, 계좌번호, 계좌 금액 원값을 남기지 않는다.
- 각 태스크는 TDD red→green, 관련 전체 테스트, 4인 리뷰 패널, 한국어 회고, 사용자 확인 후 커밋 순서로 종결한다.

---

## 파일 구조와 소유권

### 새 파일

- `backend/app/domain/notifications/models.py`: 운영자, 명령, 알림, inbox/intent/outbox 상태 값 객체와 enum
- `backend/app/domain/notifications/parsing.py`: Telegram 비의존 text command 파서
- `backend/app/domain/notifications/authorization.py`: 단일/복수 확장 가능한 운영자 인증 판정
- `backend/app/domain/notifications/formatting.py`: plain-text 메시지와 고정 chunk 렌더링
- `backend/app/domain/notifications/ports.py`: `TelegramPort`, inbox/command/outbox store port, `OperationsControlPort`
- `backend/app/domain/notifications/commands.py`: durable command intent 생성·실행·reconcile
- `backend/app/domain/notifications/projector.py`: append-only 사건→outbox 순수 매핑
- `backend/app/domain/notifications/digest.py`: 16:10 거래일 다이제스트 값 조립
- `backend/app/adapters/telegram/client.py`: `getUpdates`와 `sendMessage`
- `backend/app/core/operations_control.py`: REST/Telegram 공용 상태·계좌·제어 유스케이스
- `backend/app/core/telegram_service.py`: poller/worker/projector/sender/maintenance 생명주기
- `backend/app/store/telegram_inbox_store.py`: polling offset, poller lease, update claim
- `backend/app/store/telegram_command_store.py`: confirmation, command execution, command audit
- `backend/app/store/notification_store.py`: operational event, projector checkpoint, outbox, delivery, retention
- `backend/alembic/versions/0013_telegram_notifications.py`: Phase 8 테이블·인덱스
- `backend/tests/notifications/`: 순수 도메인·명령·projector·digest 테스트
- `backend/tests/adapters/test_telegram_client.py`: Bot API 계약 테스트
- `backend/tests/store/test_telegram_stores.py`: migration/store 동시성·복구 테스트
- `backend/tests/test_operations_control.py`: 공용 제어·계좌 snapshot 테스트
- `backend/tests/test_telegram_service.py`: 루프 격리·재기동·우선순위 테스트
- `backend/tests/test_telegram_lifespan.py`: 설정/replay/lifespan 통합 테스트
- `docs/retrospectives/2026-07-24-phase8-telegram-bot.md`: 태스크별 회고

### 수정 파일

- `backend/app/core/config.py`: Telegram all-or-nothing 설정
- `backend/app/store/models.py`: 0013 ORM 모델
- `backend/app/store/scheduler_store.py`: gave_up operational event 동시 append
- `backend/app/store/trading_store.py`: 거래 상태와 operational event 원자 기록
- `backend/app/domain/trading/service.py`: intent ID와 체결 정확도·잔량 사건 전달
- `backend/app/domain/trading/monitor.py`: 부분체결/잔량/청산 실패 사건 facts
- `backend/app/api/schedule.py`: `OperationsControl` 호출로 변경
- `backend/app/api/trade.py`: `OperationsControl` 호출로 변경
- `backend/app/main.py`: store/control/TelegramService 조립과 종료 순서
- `backend/tests/conftest.py`: 외부 Telegram 기본 비활성
- `backend/tests/test_config.py`, `backend/tests/test_api_trade.py`, `backend/tests/orchestration/test_schedule_api.py`, `backend/tests/test_app_lifespan.py`, `backend/tests/test_replay_profile.py`: 회귀
- `.env.example`: Telegram 설정과 개인 chat 안전 주석
- `docs/STATUS.md`: 태스크 진행과 최종 재개 지점

---

### Task 1: 순수 알림 모델·파서·인증·포맷

**Files:**
- Create: `backend/app/domain/notifications/__init__.py`
- Create: `backend/app/domain/notifications/models.py`
- Create: `backend/app/domain/notifications/parsing.py`
- Create: `backend/app/domain/notifications/authorization.py`
- Create: `backend/app/domain/notifications/formatting.py`
- Create: `backend/app/domain/notifications/ports.py`
- Create: `backend/tests/notifications/__init__.py`
- Create: `backend/tests/notifications/test_parsing_authorization.py`
- Create: `backend/tests/notifications/test_formatting.py`

**Interfaces:**
- Produces: `CommandKind`, `ParsedCommand`, `OperatorIdentity`, `InboundMessage`, `NotificationPriority`, `RenderedPart`
- Produces: `parse_command(text: str) -> ParsedCommand`, `is_authorized(message, operators) -> bool`, `render_parts(message, correlation_id, limit=4000) -> tuple[RenderedPart, ...]`
- Consumes: 표준 라이브러리만 사용

- [ ] **Step 1: 파서와 인증의 실패 테스트 작성**

```python
def test_confirm은_원문을_반환하지_않고_해시_재료만_검증한다():
    parsed = parse_command("/confirm AbCdEf0123456789")
    assert parsed.kind is CommandKind.CONFIRM
    assert parsed.argument == "AbCdEf0123456789"

@pytest.mark.parametrize("text", ["", "   ", "/", "/unknown", "/status extra"])
def test_빈값과_미지원명령은_InvalidCommand다(text):
    with pytest.raises(InvalidCommand):
        parse_command(text)

def test_private_user와_chat이_모두_일치해야_한다():
    allowed = (OperatorIdentity(user_id=10, chat_id=20),)
    assert is_authorized(InboundMessage(1, 10, 20, "private", "/status"), allowed)
    assert not is_authorized(InboundMessage(2, 10, 21, "private", "/status"), allowed)
    assert not is_authorized(InboundMessage(3, 10, 20, "group", "/status"), allowed)
    assert not is_authorized(
        InboundMessage(4, 10, 20, "private", "/status", forwarded=True), allowed)
```

- [ ] **Step 2: red 확인**

Run: `cd backend && uv run pytest tests/notifications/test_parsing_authorization.py -q`

Expected: FAIL with `ModuleNotFoundError: app.domain.notifications`

- [ ] **Step 3: 최소 enum·값 객체·파서·인증 구현**

```python
class CommandKind(StrEnum):
    STATUS = "status"
    ACCOUNT = "account"
    POSITIONS = "positions"
    PAUSE = "pause"
    STOP = "stop"
    RESUME = "resume"
    LIQUIDATE_ALL = "liquidate_all"
    CONFIRM = "confirm"
    HELP = "help"

class NotificationPriority(IntEnum):
    CRITICAL = 0
    NORMAL = 10
    DIGEST = 20

class InvalidCommand(ValueError):
    pass

@dataclass(frozen=True)
class ParsedCommand:
    kind: CommandKind
    argument: str | None = None

@dataclass(frozen=True)
class OperatorIdentity:
    user_id: int
    chat_id: int

@dataclass(frozen=True)
class InboundMessage:
    update_id: int
    user_id: int
    chat_id: int
    chat_type: str
    text: str
    forwarded: bool = False

@dataclass(frozen=True)
class OperationalEvent:
    kind: str
    source_type: str
    source_id: int
    version: str
    payload: dict[str, object]
    occurred_at: datetime

def is_authorized(message: InboundMessage,
                  operators: Collection[OperatorIdentity]) -> bool:
    return (
        message.chat_type == "private"
        and not message.forwarded
        and any(op.user_id == message.user_id and op.chat_id == message.chat_id
                for op in operators)
    )

def parse_command(text: str) -> ParsedCommand:
    if len(text) > 256:
        raise InvalidCommand("command text too long")
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        raise InvalidCommand("missing command")
    command = parts[0].split("@", 1)[0].removeprefix("/")
    try:
        kind = CommandKind(command)
    except ValueError as exc:
        raise InvalidCommand("unsupported command") from exc
    if kind is CommandKind.CONFIRM:
        if len(parts) != 2 or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", parts[1]):
            raise InvalidCommand("invalid confirmation token")
        return ParsedCommand(kind, parts[1])
    if len(parts) != 1:
        raise InvalidCommand("unexpected command arguments")
    return ParsedCommand(kind)
```

- [ ] **Step 4: plain-text 포맷과 고정 chunk 테스트 작성**

```python
def test_동적_문자열을_parse_mode없이_고정_chunk로_나눈다():
    parts = render_parts("종목 <b>위조</b> @admin " * 500, "evt-7", limit=200)
    assert all(part.parse_mode is None for part in parts)
    assert [part.index for part in parts] == list(range(1, len(parts) + 1))
    assert all(part.total == len(parts) and "evt-7" in part.text for part in parts)
```

- [ ] **Step 5: 포맷 구현 후 Task 1 전체 green 확인**

```python
@dataclass(frozen=True)
class RenderedPart:
    index: int
    total: int
    text: str
    parse_mode: None = None

def render_parts(message: str, correlation_id: str,
                 limit: int = 4000) -> tuple[RenderedPart, ...]:
    body_limit = limit - 64
    chunks = tuple(message[i:i + body_limit]
                   for i in range(0, len(message), body_limit)) or ("",)
    total = len(chunks)
    return tuple(RenderedPart(i, total,
                 f"[{correlation_id}] [{i}/{total}]\n{chunk}")
                 for i, chunk in enumerate(chunks, 1))
```

Run: `cd backend && uv run pytest tests/notifications -q`

Expected: PASS

- [ ] **Step 6: 리뷰·회고·커밋 후보**

```text
feat(telegram): add notification command and message domain
```

---

### Task 2: Telegram 설정과 안전한 Bot API 어댑터

**Files:**
- Modify: `backend/app/core/config.py`
- Create: `backend/app/adapters/telegram/__init__.py`
- Create: `backend/app/adapters/telegram/client.py`
- Modify: `backend/tests/test_config.py`
- Create: `backend/tests/adapters/test_telegram_client.py`
- Modify: `backend/tests/conftest.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: `Settings`, `InboundMessage`, `RenderedPart`
- Produces: `Settings.telegram_enabled`, `TelegramClient.get_updates(offset)`, `TelegramClient.send_message(chat_id, text)`

- [ ] **Step 1: 설정 all-or-nothing와 replay 비활성 테스트**

```python
def test_telegram_설정은_전부_있거나_전부_없어야_한다(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_ID", raising=False)
    with pytest.raises(ValidationError, match="TELEGRAM_"):
        Settings()

def test_telegram_enabled는_정상_3종과_non_replay에서만_true(settings_env):
    settings_env(token="secret", user="10", chat="20")
    assert Settings().telegram_enabled is True
```

- [ ] **Step 2: red 확인**

Run: `cd backend && uv run pytest tests/test_config.py -q`

Expected: FAIL because Telegram settings do not exist

- [ ] **Step 3: Settings 최소 구현**

```python
telegram_bot_token: SecretStr | None = None
telegram_allowed_user_id: int | None = None
telegram_allowed_chat_id: int | None = None

@property
def telegram_enabled(self) -> bool:
    return (self.telegram_bot_token is not None
            and self.telegram_allowed_user_id is not None
            and self.telegram_allowed_chat_id is not None
            and self.run_environment != "replay")
```

같은 validator에서 세 값의 `any != all`, ID `<= 0`을 거부한다.

- [ ] **Step 4: Bot API 보안 계약 테스트**

```python
@pytest.mark.anyio
async def test_redirect를_따르지_않고_token을_로그에_남기지_않는다(
        caplog, respx_mock):
    route = respx_mock.post(
        "https://api.telegram.org/botTOPSECRET/sendMessage"
    ).mock(return_value=httpx.Response(
        302, headers={"Location": "https://evil.example/steal"}))
    client = TelegramClient(SecretStr("TOPSECRET"))
    with pytest.raises(TelegramPermanentError):
        await client.send_message(20, "hello")
    assert route.called
    assert "TOPSECRET" not in caplog.text

@pytest.mark.anyio
async def test_401은_공유인증오류로_분류하고_추가호출하지_않는다(respx_mock):
    route = respx_mock.post(
        "https://api.telegram.org/botBAD/getUpdates"
    ).mock(return_value=httpx.Response(401, json={"ok": False}))
    client = TelegramClient(SecretStr("BAD"))
    with pytest.raises(TelegramAuthenticationError):
        await client.get_updates(0)
    with pytest.raises(TelegramAuthenticationError):
        await client.get_updates(0)
    assert route.call_count == 1
```

- [ ] **Step 5: 고정 origin client 구현**

```python
class TelegramClient:
    _ORIGIN = "https://api.telegram.org"

    def __init__(self, token: SecretStr, transport: httpx.AsyncBaseTransport | None = None):
        self._token = token
        self._http = httpx.AsyncClient(
            verify=True, follow_redirects=False, trust_env=False,
            timeout=httpx.Timeout(35.0), transport=transport)

    def _endpoint(self, method: str) -> str:
        return f"{self._ORIGIN}/bot{self._token.get_secret_value()}/{method}"

class TelegramPermanentError(RuntimeError):
    def __init__(self, endpoint: str, kind: str):
        super().__init__(f"Telegram {endpoint} failed ({kind})")
        self.endpoint = endpoint
        self.kind = kind

class TelegramRateLimited(RuntimeError):
    def __init__(self, retry_after: int):
        super().__init__("Telegram rate limited")
        self.retry_after = retry_after

class TelegramAuthenticationError(RuntimeError):
    pass
```

`get_updates()`는 `allowed_updates=["message"]`, `limit=100`, long-poll timeout
30초를 고정하고 DTO를 `InboundMessage`로 정규화한다. 모든 예외는 URL을
포함하지 않는 `endpoint=getUpdates|sendMessage` 분류로 변환한다. 401/403은
공유 authentication circuit를 열어 `TelegramAuthenticationError`로
변환하며 poller와 sender가 모두 dead가 된 뒤 동일 client는 추가 HTTP
호출을 하지 않는다.

- [ ] **Step 6: 어댑터 전체 green과 외부 호출 차단 회귀**

Run: `cd backend && uv run pytest tests/adapters/test_telegram_client.py tests/test_config.py -q`

Expected: PASS, respx 밖 네트워크 요청 0

- [ ] **Step 7: 리뷰·회고·커밋 후보**

```text
feat(telegram): add fail-fast settings and secure Bot API client
```

---

### Task 3: 0013 영속 모델과 세 저장소

**Files:**
- Create: `backend/alembic/versions/0013_telegram_notifications.py`
- Modify: `backend/app/store/models.py`
- Create: `backend/app/store/telegram_inbox_store.py`
- Create: `backend/app/store/telegram_command_store.py`
- Create: `backend/app/store/notification_store.py`
- Create: `backend/tests/store/test_telegram_stores.py`
- Modify: `backend/tests/store/test_models_migration.py`
- Modify: `backend/tests/test_migrations.py`

**Interfaces:**
- Produces: `TelegramInboxStore.persist_batch_and_offset`, `claim_next`, `release_expired`
- Produces: `IssuedConfirmation(id: int, raw_token: str)`, `TelegramCommandStore.issue_confirmation`, `consume_and_create_intent`, `claim_intent`, `mark_unknown`
- Produces: `NotificationStore.append_event`, `enqueue_outbox`, `claim_deliveries`, `finish_delivery`

- [ ] **Step 1: ORM/migration 존재와 고유키 실패 테스트**

```python
def test_update_id와_outbox_key는_중복될_수_없다(stores):
    stores.inbox.persist_batch_and_offset([update(100)], 101)
    stores.inbox.persist_batch_and_offset([update(100)], 101)
    assert stores.inbox.count_updates() == 1
    stores.notifications.enqueue_outbox("event:1:entry_filled", payload={})
    stores.notifications.enqueue_outbox("event:1:entry_filled", payload={})
    assert stores.notifications.count_outbox() == 1
```

- [ ] **Step 2: red 확인**

Run: `cd backend && uv run pytest tests/store/test_telegram_stores.py -q`

Expected: FAIL because migration/models/stores do not exist

- [ ] **Step 3: 0013 테이블과 핵심 인덱스 작성**

```python
def upgrade() -> None:
    op.create_table(
        "telegram_state",
        sa.Column("key", sa.String(32), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("lease_owner", sa.String(64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table(
        "telegram_updates",
        sa.Column("update_id", sa.BigInteger(), primary_key=True),
        sa.Column("operator_hash", sa.String(64), nullable=False),
        sa.Column("command", sa.String(24), nullable=False),
        sa.Column("argument_hash", sa.String(64), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("owner", sa.String(64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "telegram_confirmations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("operator_hash", sa.String(64), nullable=False),
        sa.Column("chat_hash", sa.String(64), nullable=False),
        sa.Column("command", sa.String(24), nullable=False),
        sa.Column("state_fingerprint", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table(
        "telegram_command_executions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("update_id", sa.BigInteger(),
                  sa.ForeignKey("telegram_updates.update_id"), nullable=False),
        sa.Column("confirmation_id", sa.BigInteger(),
                  sa.ForeignKey("telegram_confirmations.id"), nullable=True),
        sa.Column("command", sa.String(24), nullable=False),
        sa.Column("state_fingerprint", sa.String(128), nullable=False),
        sa.Column("targets_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("owner", sa.String(64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_kind", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "telegram_command_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("intent_id", sa.String(64), nullable=True),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("result", sa.String(24), nullable=False),
        sa.Column("error_kind", sa.String(64), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False))
    op.create_table(
        "telegram_rejected_update_counters",
        sa.Column("minute", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("subject_hash", sa.String(64), primary_key=True),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"))
    op.create_table(
        "operational_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("source_version", sa.String(96), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source_type", "source_id", "source_version",
            name="uq_operational_event_source_version"))
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("sensitive", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_kind", sa.String(64), nullable=True))
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("outbox_id", sa.BigInteger(),
                  sa.ForeignKey("notification_outbox.id"), nullable=False),
        sa.Column("part_index", sa.Integer(), nullable=False),
        sa.Column("total_parts", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("owner", sa.String(64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_kind", sa.String(64), nullable=True),
        sa.Column("last_http_status", sa.Integer(), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("outbox_id", "part_index",
                            name="uq_notification_delivery_part"))
    op.create_unique_constraint(
        "uq_notification_outbox_key", "notification_outbox", ["idempotency_key"])
    op.create_index(
        "ix_notification_delivery_claim",
        "notification_deliveries",
        ["status", "next_attempt_at", "lease_until", "id"])
    op.create_index(
        "ix_telegram_updates_claim", "telegram_updates",
        ["status", "lease_until", "update_id"])
    op.create_index(
        "ix_telegram_executions_claim", "telegram_command_executions",
        ["status", "lease_until", "created_at"])
    op.create_index(
        "ix_operational_events_cursor", "operational_events", ["id"])

def downgrade() -> None:
    op.drop_index("ix_operational_events_cursor",
                  table_name="operational_events")
    op.drop_index("ix_telegram_executions_claim",
                  table_name="telegram_command_executions")
    op.drop_index("ix_telegram_updates_claim",
                  table_name="telegram_updates")
    op.drop_index("ix_notification_delivery_claim",
                  table_name="notification_deliveries")
    op.drop_table("notification_deliveries")
    op.drop_constraint("uq_notification_outbox_key",
                       "notification_outbox", type_="unique")
    op.drop_table("notification_outbox")
    op.drop_table("operational_events")
    op.drop_table("telegram_rejected_update_counters")
    op.drop_table("telegram_command_audit")
    op.drop_table("telegram_command_executions")
    op.drop_table("telegram_confirmations")
    op.drop_table("telegram_updates")
    op.drop_table("telegram_state")
```

모든 상태 문자열은 `String(24)`, 시각은 timezone-aware, payload는 `Text`
JSON으로 통일한다. 원문 token/text/계좌번호 컬럼은 만들지 않는다.

- [ ] **Step 4: offset 원자성·lease 단일 승자 테스트**

```python
def test_batch와_offset은_같은_트랜잭션이다(inbox, monkeypatch):
    monkeypatch.setattr(inbox, "_insert_update", Mock(side_effect=RuntimeError))
    with pytest.raises(RuntimeError):
        inbox.persist_batch_and_offset([update(7)], 8)
    assert inbox.current_offset() == 0

def test_expired_claim만_회수한다(inbox, now):
    inbox.seed_received(1)
    first = inbox.claim_next("worker-a", lease_s=10)
    assert inbox.claim_next("worker-b", lease_s=10) is None
    now.advance(seconds=11)
    assert inbox.claim_next("worker-b", lease_s=10).update_id == first.update_id

def test_미허용폭주집계와_허용queue상한은_유계다(inbox):
    inbox.persist_rejected_batch(
        minute=NOW, subject_hashes=[f"v1:{i}" for i in range(500)],
        cardinality_limit=300)
    assert inbox.rejected_counter_rows(NOW) == 300
    inbox.seed_allowed_updates(1000)
    assert inbox.can_poll(allowed_limit=1000) is False
```

- [ ] **Step 5: confirmation 단일 소비+intent 원자 생성 테스트**

```python
def test_confirmation은_동시_소비해도_intent_한개(command_store):
    confirmation = command_store.issue_confirmation(
        operator_hash="op", chat_hash="chat", command="liquidate_all",
        state_fingerprint="fp", expires_in_s=120)
    argument_hash = hashlib.sha256(
        confirmation.raw_token.encode()).hexdigest()
    first = command_store.consume_and_create_intent(
        argument_hash=argument_hash, operator_hash="op", chat_hash="chat",
        command="liquidate_all", current_fingerprint="fp", now=NOW)
    second = command_store.consume_and_create_intent(
        argument_hash=argument_hash, operator_hash="op", chat_hash="chat",
        command="liquidate_all", current_fingerprint="fp", now=NOW)
    assert first is not None
    assert second is None
    assert command_store.intent_count(confirmation.id) == 1

def test_confirmation은_CSPRNG_2분귀속과_이전토큰무효화를_강제한다(command_store):
    first = command_store.issue_confirmation(
        "op", "chat", "resume", "fp", expires_in_s=120)
    second = command_store.issue_confirmation(
        "op", "chat", "resume", "fp", expires_in_s=120)
    assert len(base64.urlsafe_b64decode(first.raw_token + "==")) >= 24
    def consume(raw, operator, chat, command, fingerprint, now):
        return command_store.consume_and_create_intent(
            argument_hash=hashlib.sha256(raw.encode()).hexdigest(),
            operator_hash=operator, chat_hash=chat, command=command,
            current_fingerprint=fingerprint, now=now)
    assert consume(first.raw_token, "op", "chat", "resume", "fp", NOW) is None
    assert consume(second.raw_token, "other", "chat", "resume", "fp", NOW) is None
    assert consume(second.raw_token, "op", "chat", "stop", "fp", NOW) is None
    assert consume(second.raw_token, "op", "chat", "resume", "changed", NOW) is None
    assert consume(second.raw_token, "op", "chat", "resume", "fp",
                   NOW + timedelta(seconds=121)) is None
```

`issue_confirmation`은 `secrets.token_urlsafe(32)`를 사용하고 만료는 항상
120초다. 같은 operator의 새 confirmation을 만들 때 이전 미사용 행을 같은
트랜잭션에서 무효화한다. 소비 조건은 token hash, operator hash, chat hash,
command, current fingerprint, `expires_at > now`, `consumed_at IS NULL`을
모두 포함한 조건부 UPDATE다.

- [ ] **Step 6: delivery 조각·민감 payload purge·retention 테스트**

```python
def test_부분_성공_뒤_미전송_chunk만_claim한다(notification_store):
    oid = notification_store.enqueue_parts("k", ["1", "2", "3"], sensitive=True)
    notification_store.mark_sent(oid, part=1, telegram_message_id=99)
    assert [d.part_index for d in notification_store.claim_deliveries("w")] == [2, 3]
    notification_store.mark_all_sent(oid)
    assert notification_store.load_payload(oid) is None
    assert notification_store.load_delivery_bodies(oid) == [None, None, None]

def test_서로다른원천의_같은숫자버전은_충돌하지않는다(notification_store):
    notification_store.append_event(
        OperationalEvent("entry_filled", "trade_order", 7, "1", {}, NOW))
    notification_store.append_event(
        OperationalEvent("pipeline_gave_up", "scheduler_event", 7, "1", {}, NOW))
    assert notification_store.operational_event_count() == 2

def test_delivery_retry예산은_조각별이다(notification_store):
    oid = notification_store.enqueue_parts("parts", ["1", "2"], sensitive=False)
    notification_store.retry_part(oid, part=2, error_kind="timeout", http_status=None)
    first, second = notification_store.load_deliveries(oid)
    assert first.attempt_count == 0
    assert second.attempt_count == 1
    assert second.last_error_kind == "timeout"
```

- [ ] **Step 7: migration 왕복과 저장소 전체 green**

Run: `cd backend && uv run pytest tests/store/test_telegram_stores.py tests/store/test_models_migration.py tests/test_migrations.py -q`

Expected: PASS

- [ ] **Step 8: 리뷰·회고·커밋 후보**

```text
feat(telegram): add durable inbox command and outbox stores (0013)
```

---

### Task 4: REST·Telegram 공용 OperationsControl과 계좌 snapshot

**Files:**
- Create: `backend/app/core/operations_control.py`
- Modify: `backend/app/domain/trading/models.py`
- Modify: `backend/app/domain/trading/service.py`
- Modify: `backend/app/store/trading_store.py`
- Modify: `backend/app/api/schedule.py`
- Modify: `backend/app/api/trade.py`
- Create: `backend/tests/test_operations_control.py`
- Modify: `backend/tests/trading/test_service.py`
- Modify: `backend/tests/orchestration/test_schedule_api.py`
- Modify: `backend/tests/test_api_trade.py`

**Interfaces:**
- Consumes: `SchedulerService`, `TradingService | None`, `TradingStore`, `BrokerPort`, run environment
- Produces: `OperationsControl.system_status()`, `account_summary(priority: str = "interactive")`, `open_positions_summary()`, `pause_scheduler()`, `resume_scheduler(expected: str | None = None)`, `stop_new_entries(intent_id)`, `liquidate_managed(intent_id, targets)`
- Produces in `domain/trading/models.py`: `LiquidationTarget`, `LiquidationResult`
- Produces: `TradingService.request_stop_once(intent_id, mode)`, `request_managed_liquidation(intent_id, targets)`

- [ ] **Step 1: `/resume` 의미와 account source 실패 테스트**

```python
@pytest.mark.anyio
async def test_resume은_scheduler_pause만_해제한다(control, trading):
    control.scheduler.pause()
    trading.request_stop(StopMode.STOP_NEW_ENTRIES)
    await control.resume_scheduler(expected=control.scheduler_fingerprint())
    assert control.scheduler.paused is False
    assert trading.stop_requested() is StopMode.STOP_NEW_ENTRIES

@pytest.mark.anyio
async def test_account는_deposit과_balance를_같이_조회하고_총수익률을_만들지_않는다(
        control):
    summary = await control.account_summary()
    assert summary.available_deposit == 1_000_000
    assert summary.total_eval == 1_200_000
    assert summary.total_return_rate is None

@pytest.mark.anyio
async def test_balance실패에도_deposit과_실현손익은_유지한다(control, broker):
    broker.balance_error = BrokerError("down")
    summary = await control.account_summary()
    assert summary.available_deposit == 1_000_000
    assert summary.total_eval is None
    assert summary.realized_pnl == -12_000
    assert summary.realized_pnl_confidence == "estimated"
    assert summary.trading_day == date(2026, 7, 24)
    assert summary.failed_fields == ("balance",)
```

- [ ] **Step 2: red 확인**

Run: `cd backend && uv run pytest tests/test_operations_control.py -q`

Expected: FAIL because `OperationsControl` does not exist

- [ ] **Step 3: 공용 snapshot과 10초 single-flight 구현**

```python
@dataclass(frozen=True)
class AccountSummary:
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

@dataclass(frozen=True)
class LiquidationPreview:
    targets: tuple[LiquidationTarget, ...]
    managed_symbols: tuple[str, ...]
    unmanaged_symbols: tuple[str, ...]

class OperationsControl:
    async def account_summary(self, priority: str = "interactive") -> AccountSummary:
        async with self._account_lock:
            if self._cache and self._now() - self._cache_at < timedelta(seconds=10):
                return self._cache
            if self._account_task is None or self._account_task.done():
                self._account_task = asyncio.create_task(self._load_account())
            return await asyncio.shield(self._account_task)

    async def _load_account(self) -> AccountSummary:
        deposit, balance = await asyncio.gather(
            self._broker.get_deposit(), self._broker.get_balance(),
            return_exceptions=True)
        realized, confidence = self._store.realized_pnl_today(
            self._run_environment, self._now())
        failed = []
        if isinstance(deposit, Exception):
            failed.append("deposit")
        if isinstance(balance, Exception):
            failed.append("balance")
        self._cache = AccountSummary(
            available_deposit=(None if isinstance(deposit, Exception)
                               else deposit.available),
            total_eval=(None if isinstance(balance, Exception)
                        else balance.total_eval),
            total_profit=(None if isinstance(balance, Exception)
                          else balance.total_profit),
            total_return_rate=None,
            realized_pnl=realized,
            realized_pnl_confidence=confidence,
            trading_day=self._now().astimezone(self._calendar.KST).date(),
            as_of=self._now(),
            source="broker+trade_store",
            failed_fields=tuple(failed))
        return self._cache
```

`LiquidationTarget`과 `LiquidationResult`는 core가 아니라
`backend/app/domain/trading/models.py`가 소유한다. TradingService와
OperationsControl은 같은 domain 값 객체를 import한다.

```python
@dataclass(frozen=True)
class LiquidationTarget:
    position_id: int
    symbol: str
    quantity: int

@dataclass(frozen=True)
class LiquidationResult:
    status: str
    account_fully_empty: bool
    warning: str | None
```

당일 realized P&L에는 `estimated|broker_reconciled` 정확도와 KST 귀속일을
붙이고, 한 소스 실패 시 나머지 필드와 `failed_fields`를 반환한다.

- [ ] **Step 4: 관리/미관리 포지션 대사와 멱등 청산 테스트**

```python
@pytest.mark.anyio
async def test_liquidate_all은_관리분만_대상이고_미관리잔고를_경고한다(control):
    preview = await control.liquidation_preview()
    assert preview.managed_symbols == ("005930",)
    assert preview.unmanaged_symbols == ("000660",)
    result = await control.liquidate_managed("intent-1", preview.targets)
    assert result.account_fully_empty is False
    assert "계좌 전체 잔고 0 아님" in result.warning
```

- [ ] **Step 5: TradingService에 intent·target 소유 계약 추가**

```python
async def request_managed_liquidation(
        self, intent_id: str,
        targets: tuple[LiquidationTarget, ...]) -> LiquidationResult:
    if self._managed_liquidation_intent == intent_id:
        return await self.reconcile_control_intent(intent_id)
    current = self._store.open_positions_by_ids(
        [target.position_id for target in targets], self._run_environment)
    if {(p.id, p.symbol, p.quantity) for p in current} != {
            (t.position_id, t.symbol, t.quantity) for t in targets}:
        return LiquidationResult("needs_attention", False,
                                 "confirmed target state changed")
    self._managed_liquidation_targets = frozenset(t.position_id for t in targets)
    self._managed_liquidation_intent = intent_id
    await self.request_stop_durable(StopMode.LIQUIDATE_ALL)
    return await self.reconcile_control_intent(intent_id)
```

durable idempotency SSOT는 Task 5의 `telegram_command_executions`이며
TradingService는 전달받은 intent ID를 실행 수명 동안 보조 가드와 감사
상관 ID로 사용한다. 재기동한 `unknown` intent는 command worker가 먼저
broker/주문 DB를 대사하고, 새 부수효과가 필요하다고 판정한 경우에만 같은
intent ID로 서비스에 진입한다.

`_liquidate_all()`은 `_managed_liquidation_targets`에 속한 현재 환경 포지션만
처리한다. 각 target은 기존 청산 미체결 주문을 먼저 대사하고 중복 매도하지
않는다. `request_stop_once(intent_id, STOP_NEW_ENTRIES)`도 현재 run의
durable kill-switch mode와 command execution을 대사해 같은 정지 요청을
반복하지 않는다.

```python
@pytest.mark.parametrize(
    ("broker_qty", "open_orders", "position_state", "expected"),
    [(0, 0, "closed", "succeeded"),
     (7, 1, "exiting", "needs_attention"),
     (7, 0, "exit_failed", "needs_attention"),
     (7, 0, "entered", "needs_attention")])
@pytest.mark.anyio
async def test_managed_liquidation_terminal조건(
        trading, broker_qty, open_orders, position_state, expected):
    trading.seed_liquidation_state(broker_qty, open_orders, position_state)
    result = await trading.reconcile_control_intent("intent-1")
    assert result.status == expected
```

거래정지·장 종료·broker 조회 실패·미체결 소멸 뒤 잔고 잔존을 별도 fixture로
검증하고 모두 종목·잔량·미체결 상태·수동 조치가 있는
`needs_attention` 결과인지 단언한다.

- [ ] **Step 6: REST 라우터를 공용 control로 전환하고 회귀 green**

```python
@router.post("/schedule/resume", dependencies=[Depends(require_trade_token)])
async def schedule_resume(request: Request) -> dict:
    result = await request.app.state.operations_control.resume_scheduler()
    return {"paused": result.paused}
```

HTTP 인증·409/422/503 변환은 API 계층에 남기고 의미 판정은 control 결과로
매핑한다.

Run: `cd backend && uv run pytest tests/test_operations_control.py tests/orchestration/test_schedule_api.py tests/test_api_trade.py -q`

Expected: PASS and existing REST response contract unchanged

- [ ] **Step 7: 리뷰·회고·커밋 후보**

```text
refactor(ops): share safe control use cases across REST and Telegram
```

---

### Task 5: durable 수신·확인·제어 command worker

**Files:**
- Create: `backend/app/domain/notifications/commands.py`
- Create: `backend/tests/notifications/test_commands.py`
- Modify: `backend/app/store/telegram_command_store.py`
- Modify: `backend/tests/store/test_telegram_stores.py`

**Interfaces:**
- Consumes: `TelegramInboxStore`, `TelegramCommandStore`, `OperationsControlPort`, parser/auth 결과
- Produces: `CommandProcessor.process_next()`, `reconcile_unknown()`

- [ ] **Step 1: 즉시 명령도 intent 선행, 위험 명령은 확인만 발급하는 테스트**

```python
@pytest.mark.anyio
async def test_stop은_intent가_먼저_영속된_뒤_control을_호출한다(processor, calls):
    await processor.process(update("/stop", update_id=10))
    assert calls == ["intent:10", "control:stop:intent:10", "audit:succeeded"]

@pytest.mark.anyio
async def test_liquidate첫요청은_청산하지_않고_confirmation만_발급한다(
        processor, control):
    result = await processor.process(update("/liquidate_all", update_id=11))
    assert result.kind == "confirmation_required"
    assert control.liquidate_calls == []
```

- [ ] **Step 2: red 확인**

Run: `cd backend && uv run pytest tests/notifications/test_commands.py -q`

Expected: FAIL because `CommandProcessor` does not exist

- [ ] **Step 3: inbox와 execution 상태 머신 구현**

```python
async def process_next(self) -> None:
    claimed = self._inbox.claim_next(self._worker_id, lease_s=15)
    if claimed is None:
        return
    try:
        if claimed.command is CommandKind.CONFIRM:
            intent = self._commands.consume_and_create_intent(
                claimed.argument_hash, self._control.fingerprint_for(claimed))
        else:
            intent = self._commands.create_intent_for_update(claimed)
        await self._execute(intent)
        self._inbox.complete(claimed.update_id, claimed.version)
    except Exception:
        self._inbox.reject_or_release(claimed.update_id, claimed.version)
        raise
```

`claimed` lease 만료는 received/pending으로만 되돌리고, `running` intent는
unknown→reconciling을 거쳐 terminal로만 종결한다.

- [ ] **Step 4: 크래시 대사·중복 매도 방지 테스트**

```python
@pytest.mark.anyio
async def test_running_liquidation재기동은_기존미체결을_재발주하지_않는다(
        processor, broker, orders):
    processor.seed_unknown_liquidation("intent-7", symbol="005930", qty=3)
    broker.open_orders = [sell_order("005930", 3)]
    await processor.reconcile_unknown()
    assert orders.place_sell_calls == []
    assert processor.intent_status("intent-7") == "needs_attention"
```

- [ ] **Step 5: 동시 confirm 단일 승자·상태 지문 경합 테스트**

Run: `cd backend && uv run pytest tests/notifications/test_commands.py tests/store/test_telegram_stores.py -q`

Expected: PASS, simultaneous confirm produces one intent

- [ ] **Step 6: 모든 명령 분기와 민감 응답 outbox 테스트**

```python
@pytest.mark.parametrize(
    ("text", "expected"),
    [("/status", "status"), ("/account", "account"),
     ("/positions", "positions"), ("/pause", "pause"),
     ("/stop", "stop"), ("/resume", "confirmation_required"),
     ("/liquidate_all", "confirmation_required"), ("/help", "help")])
@pytest.mark.anyio
async def test_지원명령은_명시된_결과로_수렴(processor, text, expected):
    result = await processor.process(update(text))
    assert result.kind == expected
    if text in {"/account", "/positions"}:
        assert result.outbox_sensitive is True
```

Run: `cd backend && uv run pytest tests/notifications/test_commands.py -q`

Expected: PASS

- [ ] **Step 7: 리뷰·회고·커밋 후보**

```text
feat(telegram): add durable and reconcilable command processing
```

---

### Task 6: append-only operational events와 체결 정확도

**Files:**
- Modify: `backend/app/store/scheduler_store.py`
- Modify: `backend/app/store/trading_store.py`
- Modify: `backend/app/domain/trading/service.py`
- Modify: `backend/app/domain/trading/monitor.py`
- Create: `backend/app/domain/notifications/projector.py`
- Create: `backend/tests/notifications/test_projector.py`
- Modify: `backend/tests/orchestration/test_scheduler_store.py`
- Modify: `backend/tests/trading/test_observability.py`
- Modify: `backend/tests/trading/test_monitor.py`

**Interfaces:**
- Produces operational event kinds: `entry_partial_fill`, `entry_filled`, `exit_partial_fill`, `exit_filled`, `exit_unconfirmed`, `exit_remaining_failed`, `kill_switch_*`, `pipeline_gave_up`, `scheduler_dead`
- Consumes: `NotificationStore.append_event_in_session(session, event)`
- Produces: `NotificationProjector.project_batch(limit=100) -> int`

- [ ] **Step 1: scheduler gave_up 원자 사건 테스트**

```python
def test_gave_up은_scheduler_event와_operational_event를_같이_남긴다(store):
    store.record_event(Job.TRADE, Action.GAVE_UP, Reason.WINDOW_EXPIRED)
    event = store.latest_operational_event()
    assert event.kind == "pipeline_gave_up"
    assert event.payload["notification_kinds"] == [
        "pipeline_gave_up", "trading_monitoring_gap"]
```

- [ ] **Step 2: 거래 부분체결·잔량 사건 실패 테스트**

```python
def test_partial_exit_event는_누적과_잔량과_정확도를_보존한다(trading_store):
    trading_store.record_fill_event(
        order_id=4, kind="exit_partial_fill", order_qty=10,
        fill_qty=3, cumulative_fill_qty=3, remaining_qty=7,
        avg_fill_price=70_000, price_confidence="estimated",
        remaining_order_state="open")
    event = trading_store.latest_operational_event()
    assert event.payload["remaining_qty"] == 7
    assert event.payload["price_confidence"] == "estimated"
    assert event.payload["remaining_order_state"] == "open"
```

- [ ] **Step 3: red 확인**

Run: `cd backend && uv run pytest tests/orchestration/test_scheduler_store.py tests/trading/test_observability.py tests/trading/test_monitor.py -q`

Expected: FAIL because operational event writes are absent

- [ ] **Step 4: 소유 store의 같은 트랜잭션 append 구현**

```python
with self._sessions.begin() as session:
    row = SchedulerEventRow(
        ts=self._now(), job=job.value, action=action.value,
        reason=reason.value, run_id=run_id)
    session.add(row)
    session.flush()
    if action is Action.GAVE_UP:
        self._notifications.append_event_in_session(
            session,
            OperationalEvent(
                kind="pipeline_gave_up",
                source_type="scheduler_event",
                source_id=row.id,
                version=f"{row.id}:pipeline_gave_up",
                payload={
                    "job": job.value,
                    "reason": reason.value,
                    "run_id": run_id,
                    "notification_kinds": (
                        ["pipeline_gave_up", "trading_monitoring_gap"]
                        if job is Job.TRADE else ["pipeline_gave_up"]),
                }))
```

TradingStore의 position/order/fill 상태 변경도 같은 session에서 사건을
append한다. DB 변경이 rollback되면 사건도 없어야 한다.

- [ ] **Step 5: monitor/service에서 정확한 fill facts 전달**

```python
self._store.record_fill_event(
    order_id=order_id,
    kind="exit_partial_fill" if remaining else "exit_filled",
    order_qty=original_qty,
    fill_qty=filled_now,
    cumulative_fill_qty=filled_total,
    remaining_qty=remaining,
    avg_fill_price=observed_or_estimated,
    price_confidence=("broker_reconciled"
                      if balance_confirmed else "estimated"),
    remaining_order_state=remaining_order_state)
```

브로커가 fill 세부를 제공하지 않는 경로는 추정치를 확정값으로 바꾸지 않고
`exit_unconfirmed`를 남긴다. `EXIT_FAILED`와 장 마감 잔량은
`exit_remaining_failed`다. `remaining_order_state`는 vendor-neutral
`open|cancel_pending|cancelled|none|unknown` 중 하나이며 부분체결의 모든
projector 메시지에 필수다.

```python
@pytest.mark.parametrize(
    "remaining_order_state",
    ["open", "cancel_pending", "cancelled", "unknown"])
def test_partial_fill메시지는_미체결상태를_반드시_표시한다(
        projector, remaining_order_state):
    message = projector.project_partial_fill(
        remaining_qty=7, remaining_order_state=remaining_order_state)
    assert remaining_order_state in message.text
```

- [ ] **Step 6: 단일 ID cursor projector red/green**

```python
def test_projector는_checkpoint와_outbox를_원자적으로_전진한다(projector):
    projector.seed_event(id=9, kind="entry_filled")
    assert projector.project_batch() == 1
    assert projector.checkpoint() == 9
    projector.rewind_checkpoint(8)
    assert projector.project_batch() == 0
    assert projector.outbox_count("operational:9:entry_filled") == 1
```

Run: `cd backend && uv run pytest tests/notifications/test_projector.py tests/orchestration/test_scheduler_store.py tests/trading/test_observability.py tests/trading/test_monitor.py -q`

Expected: PASS

- [ ] **Step 7: 리뷰·회고·커밋 후보**

```text
feat(telegram): emit durable trading and scheduler notification events
```

---

### Task 7: outbox sender, 재시도, 지연·분할 전달

**Files:**
- Create: `backend/app/core/telegram_service.py` (sender 하위 컴포넌트부터)
- Modify: `backend/app/domain/notifications/formatting.py`
- Modify: `backend/app/store/notification_store.py`
- Create: `backend/tests/test_telegram_service.py`
- Modify: `backend/tests/notifications/test_formatting.py`
- Modify: `backend/tests/store/test_telegram_stores.py`

**Interfaces:**
- Consumes: `TelegramPort.send_message`, `NotificationStore.claim_deliveries`
- Produces: `OutboxSender.run_once()`, `OutboxSender.snapshot()`

- [ ] **Step 1: 429/5xx/영구4xx 상태 전이 테스트**

```python
@pytest.mark.anyio
async def test_429는_retry_after를_따른다(sender, telegram, now):
    telegram.fail_with_rate_limit(retry_after=17)
    await sender.run_once()
    delivery = sender.latest_delivery()
    assert delivery.status == "pending"
    assert delivery.next_attempt_at == now + timedelta(seconds=17)

@pytest.mark.anyio
async def test_400은_즉시_dead_letter(sender, telegram):
    telegram.fail_permanently(400)
    await sender.run_once()
    assert sender.latest_outbox().status == "dead_letter"
```

- [ ] **Step 2: red 확인**

Run: `cd backend && uv run pytest tests/test_telegram_service.py -q`

Expected: FAIL because `OutboxSender` does not exist

- [ ] **Step 3: lease·우선순위·retry 구현**

```python
async def run_once(self) -> int:
    deliveries = await asyncio.to_thread(
        self._store.claim_deliveries, self._worker_id, 20)
    for delivery in deliveries:
        try:
            message_id = await self._telegram.send_message(
                self._chat_id, delivery.text)
        except TelegramRateLimited as exc:
            self._store.retry(delivery.id, delay_s=exc.retry_after)
        except TelegramAuthenticationError:
            self._shared_circuit.mark_dead("authentication_failed")
            raise
        except TelegramPermanentError as exc:
            self._store.dead_letter(delivery.id, exc.kind)
        else:
            self._store.mark_sent(delivery.id, message_id)
    return len(deliveries)
```

claim 정렬은 긴급 priority, 발생 시각, part index 순이다. 일반 retry는
지수 backoff+jitter다. delivery 자신의 `attempt_count >= 10` 또는
`now - created_at >= 24시간`이면 그 조각과 부모 outbox를 dead-letter로
종결한다. 만료된 sending lease는 delivery 단위로 pending 회수한다.

- [ ] **Step 4: 지연 표시·부분 성공 재시도·민감 purge 테스트**

```python
@pytest.mark.anyio
async def test_5분지난긴급알림은_지연표시하고_성공chunk를_다시안보낸다(sender):
    sender.seed_three_parts(occurred_at=sender.now - timedelta(minutes=6))
    sender.telegram.fail_part_once(2)
    await sender.run_until_idle()
    assert sender.telegram.sent_part_counts == {1: 1, 2: 2, 3: 1}
    assert sender.telegram.messages[0].startswith("[지연 알림]")
    assert sender.store.sensitive_payload() is None
    assert sender.store.sensitive_delivery_bodies() == [None, None, None]
```

- [ ] **Step 5: sender 전체 green**

Run: `cd backend && uv run pytest tests/test_telegram_service.py tests/notifications/test_formatting.py tests/store/test_telegram_stores.py -q`

Expected: PASS

- [ ] **Step 6: 리뷰·회고·커밋 후보**

```text
feat(telegram): deliver durable notifications with bounded retries
```

---

### Task 8: 16:10 다이제스트와 보존 maintenance

**Files:**
- Create: `backend/app/domain/notifications/digest.py`
- Modify: `backend/app/core/telegram_service.py`
- Modify: `backend/app/store/notification_store.py`
- Create: `backend/tests/notifications/test_digest.py`
- Modify: `backend/tests/test_telegram_service.py`

**Interfaces:**
- Consumes: market calendar, run stores, `OperationsControl.account_summary(priority="low")`, outbox
- Produces: `DigestPlanner.due_dates(now)`, `DigestBuilder.build(date)`, `Maintenance.run_once()`

- [ ] **Step 1: 16:10·7거래일 캐치업·비거래일 테스트**

```python
def test_digest는_1610부터_최근7거래일을_오래된순으로_캐치업(planner):
    planner.mark_generated(date(2026, 7, 22))
    assert planner.due_dates(kst(2026, 7, 24, 16, 9)) == ()
    assert planner.due_dates(kst(2026, 7, 24, 16, 10)) == (
        date(2026, 7, 23), date(2026, 7, 24))
```

- [ ] **Step 2: red 확인**

Run: `cd backend && uv run pytest tests/notifications/test_digest.py -q`

Expected: FAIL because digest module does not exist

- [ ] **Step 3: planner와 DB-only fallback 구현**

```python
async def build(self, trading_day: date) -> Digest:
    try:
        account = await asyncio.wait_for(
            self._control.account_summary(priority="low"), timeout=5)
    except (TimeoutError, BrokerError):
        account = AccountSummary.unavailable("broker lookup failed")
    return Digest(
        trading_day=trading_day,
        pipeline=self._runs.pipeline_summary(trading_day),
        trading=self._runs.trade_summary(trading_day),
        account=account)
```

날짜 멱등 키는 `digest:{run_environment}:{YYYY-MM-DD}`다. 7거래일보다 오래된
누락은 `digest_skipped_stale` audit로 종결한다.

- [ ] **Step 4: 03:30 retention과 bounded delete 테스트**

```python
def test_maintenance는_민감만료와_보존기간을_batch로_정리한다(store):
    store.seed_expired(rows=1500)
    account_id = store.seed_sensitive(
        kind="account_response", age=timedelta(minutes=15),
        status="pending", payload="예수금 1000000")
    digest_id = store.seed_sensitive(
        kind="digest", age=timedelta(hours=24),
        status="dead_letter", payload="총평가 1200000")
    deleted = store.cleanup(now=fixed_now, batch_size=500)
    assert deleted == 500
    assert store.load_payload(account_id) is None
    assert store.load_delivery_bodies(account_id) == [None]
    assert store.load_payload(digest_id) is None
    assert store.load_delivery_bodies(digest_id) == [None]
    assert store.load_metadata(account_id).status == "dead_letter"
```

민감 본문 TTL은 상태와 무관하게 적용한다. 메타 행은 보존하지만
`pending/sending/dead_letter` 모두 account/positions 응답은 15분,
다이제스트는 24시간에 outbox payload와 모든 delivery body를 같은
트랜잭션에서 NULL 처리한다.

- [ ] **Step 5: digest/maintenance green**

Run: `cd backend && uv run pytest tests/notifications/test_digest.py tests/test_telegram_service.py -q`

Expected: PASS

- [ ] **Step 6: 리뷰·회고·커밋 후보**

```text
feat(telegram): add market-close digest and retention maintenance
```

---

### Task 9: TelegramService 전체 루프와 FastAPI lifespan 조립

**Files:**
- Modify: `backend/app/core/telegram_service.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/test_telegram_lifespan.py`
- Modify: `backend/tests/test_app_lifespan.py`
- Modify: `backend/tests/test_replay_profile.py`
- Modify: `backend/tests/conftest.py`

**Interfaces:**
- Consumes: Tasks 2~8의 client/stores/control/processor/projector/sender/digest
- Produces: `TelegramService.start()`, `begin_shutdown()`, `finish_shutdown(deadline_s=10)`, `snapshot()`
- Produces app state: `operations_control`, `telegram_service | None`

- [ ] **Step 1: 설정 없음/replay/정상 조립 테스트**

```python
def test_설정없음은_telegram비활성(settings):
    with TestClient(create_app(settings)) as client:
        assert client.app.state.telegram_service is None

def test_replay는_telegram설정이_있어도_강제비활성(replay_settings):
    with replay_server(), TestClient(create_app(replay_settings)) as client:
        assert client.app.state.telegram_service is None
```

- [ ] **Step 2: red 확인**

Run: `cd backend && uv run pytest tests/test_telegram_lifespan.py -q`

Expected: FAIL because app state/lifespan wiring does not exist

- [ ] **Step 3: InboxPoller와 제어/조회 dispatcher red/green**

```python
@pytest.mark.anyio
async def test_poller는_300초과를_고정비집계하고_1000상한에서_backoff(poller):
    poller.seed_untrusted_updates(301)
    await poller.run_once()
    assert poller.rejected_counter_rows() <= 300
    assert poller.rejected_total() == 301
    poller.seed_allowed_queue(size=1000)
    await poller.run_once()
    assert poller.telegram_get_updates_calls == 1
    assert poller.backoff_reason == "allowed_queue_full"

@pytest.mark.anyio
async def test_제어는_update순서를_지키고_느린조회와_분리된다(dispatcher):
    dispatcher.seed("/account", update_id=10, broker_delay_s=30)
    dispatcher.seed("/stop", update_id=11)
    await dispatcher.tick_control()
    assert dispatcher.control_intent_ids == ["intent:11"]
    assert dispatcher.query_queue_depth == 1
```

```python
class InboxPoller:
    async def run_once(self) -> None:
        if self._inbox.allowed_queue_size() >= 1000:
            self._state.backoff("allowed_queue_full")
            return
        if not self._inbox.acquire_poller_lease(self._worker_id, lease_s=40):
            return
        updates = await self._telegram.get_updates(self._inbox.offset())
        batch = self._normalize(updates, max_total=300)
        self._inbox.persist_batch_counters_and_offset(batch)
```

조회 queue는 동시성 1, 최대 20이며 `/account`·`/positions`는
OperationsControl의 10초 cooldown/single-flight를 공유한다. 제어 lane은
허용 제어 update끼리 `update_id` 순서를 지키되 조회 작업을 기다리지 않는다.
외부 ID는 `HMAC-SHA-256(bot_token, "v1:{kind}:{id}")`으로 저장하고
`v1:` key version을 붙인다. bot token 원문과 ID 원문은 저장하지 않는다.

Run: `cd backend && uv run pytest tests/test_telegram_service.py -q`

Expected: PASS

- [ ] **Step 4: 독립 루프 supervisor 구현**

```python
class TelegramService:
    def start(self) -> None:
        self._tasks = {
            "poller": asyncio.create_task(self._poll_loop()),
            "commands": asyncio.create_task(self._command_loop()),
            "projector": asyncio.create_task(self._project_loop()),
            "sender": asyncio.create_task(self._send_loop()),
            "maintenance": asyncio.create_task(self._maintenance_loop()),
        }
        for name, task in self._tasks.items():
            task.add_done_callback(partial(self._on_done, name))
```

각 loop는 trading/scheduler로 예외를 전파하지 않고 예산 소진 시 하위
`dead` 상태와 비민감 오류 분류를 snapshot에 남긴다.

- [ ] **Step 5: 종료 소유권과 10초 상한 테스트**

```python
@pytest.mark.anyio
async def test_service는_자기하위루프만_종료한다(service):
    await service.begin_shutdown()
    await service.finish_shutdown(deadline_s=10)
    assert service.stop_trace == [
        "poller", "inbox_commit", "command_claims", "projector", "sender"]

def test_main이_전체종료순서의_유일한소유자(app_shutdown_trace):
    assert app_shutdown_trace == [
        "telegram_begin", "scheduler", "trading", "telegram_finish"]
```

`TelegramService`는 scheduler/trading의 생명주기를 소유하거나 중단하지
않는다. scheduler dead 상태는 주입된 읽기 전용 snapshot port로 관찰한다.
`main.py` lifespan만 전체 종료 순서를 소유한다:
`telegram.begin_shutdown()` → scheduler stop → trading stop →
`telegram.finish_shutdown(remaining_deadline=10)`. 10초 상한은 Telegram
추가 drain에만 적용하며 trading 원자 사이클의 기존 종료 정책을 줄이지 않는다.

- [ ] **Step 6: poller 폭주·허용 명령 지연·외부 장애 격리 테스트**

```python
@pytest.mark.anyio
async def test_미허용폭주는_집계되고_stop은_버려지지않는다(service):
    service.telegram.seed_untrusted(500)
    service.telegram.seed_allowed("/stop")
    await service.tick_until_idle()
    assert service.rejected_counter() == 500
    assert service.control.stop_calls == 1
    assert service.scheduler_ticks > 0
    assert service.monitor_polls == service.expected_monitor_polls

@pytest.mark.anyio
async def test_stop_5초SLO와_초과경고를_주입시계로_검증한다(service, clock):
    service.telegram.seed_allowed("/stop", received_at=clock.now())
    clock.advance(seconds=4)
    await service.tick_control()
    assert service.control.stop_calls == 1
    assert service.snapshot()["oldest_control_age_s"] == 4
    service.seed_claim_delay(seconds=6)
    await service.tick_control()
    assert service.snapshot()["control_delay_warning"] is True

@pytest.mark.anyio
async def test_scheduler_dead_false_to_true는_긴급사건을_한번만_남긴다(service):
    service.scheduler.set_dead(False)
    await service.observe_scheduler()
    service.scheduler.set_dead(True)
    await service.observe_scheduler()
    await service.observe_scheduler()
    assert service.operational_event_count("scheduler_dead") == 1

@pytest.mark.anyio
async def test_telegram인증실패는_poller_sender공유dead이고_추가호출0(service):
    service.telegram.fail_authentication()
    await service.tick_all()
    await service.tick_all()
    assert service.snapshot()["poller"]["dead"] is True
    assert service.snapshot()["sender"]["dead"] is True
    assert service.telegram.calls == 1
    assert service.snapshot()["last_error_kind"] == "authentication_failed"
```

- [ ] **Step 7: lifespan/replay/기존 전체 관련 green**

Run: `cd backend && uv run pytest tests/test_telegram_lifespan.py tests/test_app_lifespan.py tests/test_replay_profile.py tests/orchestration/test_schedule_api.py tests/test_api_trade.py -q`

Expected: PASS, no external network call

- [ ] **Step 8: 리뷰·회고·커밋 후보**

```text
feat(telegram): wire resilient bot lifecycle into FastAPI
```

---

### Task 10: 전체 회귀·모의 수용 준비·운영 문서

**Files:**
- Modify: `.env.example`
- Modify: `docs/STATUS.md`
- Create/Finalize: `docs/retrospectives/2026-07-24-phase8-telegram-bot.md`
- Modify: `docs/architecture/system-overview.md`
- Test: all `backend/tests/` except live markers

**Interfaces:**
- Consumes: 완성된 Phase 8 기능
- Produces: 운영 절차, 수용 체크리스트, 재개 지점

- [ ] **Step 1: 비밀·외부 호출·설정 회귀 검사 추가**

```python
def test_로그에_telegram_token_confirmation_계좌금액이_없다(caplog, app):
    exercise_telegram_paths(app)
    assert "TOPSECRET" not in caplog.text
    assert "confirm-raw" not in caplog.text
    assert "123456789" not in caplog.text
```

- [ ] **Step 2: 전체 backend 검증**

Run: `cd backend && uv run pytest`

Expected: 모든 비-live 테스트 PASS, live markers 11개 deselected, 외부 호출 0

- [ ] **Step 3: migration 검증**

Run: `cd backend && uv run alembic upgrade head`

Expected: `0012 -> 0013` 적용 성공

Run: `cd backend && uv run alembic downgrade 0012 && uv run alembic upgrade head`

Expected: downgrade/upgrade 왕복 성공. 프로덕션 DB에서는 downgrade하지 않고
임시 검증 DB에서만 실행한다.

- [ ] **Step 4: 모의 수용 절차 문서화**

```text
1. 전용 테스트 bot token과 개인 user/chat ID를 .env에 설정한다.
2. 백엔드는 모의 KIWOOM_MOCK=true, 단일 컨테이너/worker로 기동한다.
3. /status, /account, /positions를 확인한다.
4. /pause 즉시 실행과 /resume 2단계 확인을 검증한다.
5. 소액 OhMyStock 관리 포지션을 모의계좌에 준비하고 DB 포지션·브로커 잔고를 대사한다.
6. /stop 즉시 실행과 /liquidate_all preview/confirm을 검증한다.
7. 정상 청산은 관리 대상 broker 잔고 0·미체결 0·intent=succeeded를 대사한다.
8. 리플레이 fault seam으로 부분체결, 미체결, 거래정지, 장 종료를 각각
   결정적으로 유도해 remaining_order_state·잔량·가격 정확도·수동 조치
   문구와 intent=needs_attention을 DB/Telegram 양쪽에서 대사한다.
9. 모의 API에서 결정적으로 만들 수 있는 실제 체결 경로와 리플레이 fault
   근거를 별도 증거 파일로 구분하고 리플레이 결과를 실서버 실측으로 표현하지 않는다.
10. Telegram 네트워크를 차단해 outbox를 누적하고 복구 후 [지연 알림]을 확인한다.
11. 프로세스를 sender 성공 직전/직후 재기동해 같은 상관 ID의 허용된 중복을 확인한다.
12. 16:10 또는 주입 시계로 다이제스트 날짜 멱등성을 확인한다.
13. SQL 감사와 로그에 token·계좌번호·금액 원값이 없는지 확인한다.
```

실제 Telegram/키움 호출은 이 문서 작성 태스크에서 실행하지 않는다. 별도
수용 실행은 사용자 승인과 명시적 범위가 있을 때만 한다.

- [ ] **Step 5: Phase 8 회고와 STATUS 갱신**

회고에는 요청, 기존 상태, 태스크별 설계 판단, 변경 파일·정확한 위치,
각 패널 발견과 수정, 검증 명령·결과, 미실행 live 검증, 운영 한계를 기록한다.

- [ ] **Step 6: 최종 4인 패널과 필요 관점 재검토**

Critical/Important를 수정하고 해당 관점 승인 후 전체 테스트를 다시 실행한다.
이 Phase는 기존 키움 TR 요청·응답 형식을 변경하지 않으므로 기본적으로
`broker-api-expert`는 제외한다. 구현 중 broker adapter/TR/주문 호출 계약을
건드리면 즉시 조건부 리뷰어를 추가한다.

- [ ] **Step 7: 최종 커밋 후보**

```text
docs(retro): record Phase 8 Telegram bot verification
```

---

## 태스크 의존 순서

```text
Task 1 → Task 2
Task 1 → Task 3
Task 3 → Task 4 → Task 5
Task 3 → Task 6 → Task 7
Task 4 + Task 7 → Task 8
Task 2 + Task 5 + Task 6 + Task 7 + Task 8 → Task 9
Task 9 → Task 10
```

Task 2와 Task 3은 Task 1 뒤 병렬로 구현할 수 있지만, 프로젝트 규칙상 각
태스크의 리뷰·회고·커밋 경계를 유지한다. Task 5와 Task 6도 파일 충돌이
없는 범위에서 병렬 가능하나 `TradingStore`와 `OperationsControl` 인터페이스를
먼저 고정해야 한다.

## 완료 게이트

- 스펙 §1~§14의 각 요구가 위 Task 1~10 중 하나 이상의 테스트·수용 항목에 연결된다.
- 기본 `uv run pytest`가 외부 Telegram·키움 호출 없이 통과한다.
- 0013 migration upgrade와 임시 DB downgrade/upgrade 왕복이 통과한다.
- 4인 패널의 Critical/Important가 0이고 조건부 broker 리뷰가 필요했다면 승인된다.
- 실제 Bot API·키움 모의 수용은 사용자 승인 전 실행하지 않는다.
- 커밋 전마다 전체 메시지와 포함 파일을 사용자에게 제시한다.

## 계획 리뷰 기록

2026-07-24에 `senior-developer`, `senior-trader`,
`architecture-expert`, `security-expert`가 같은 계획을 독립 검토했다.
키움 TR·브로커 어댑터 변경을 요구하지 않는 계획이라
`broker-api-expert`는 추가하지 않았다.

초기 리뷰는 공용 제어와 TradingService 청산 계약의 단절, 계좌 부분 실패,
operational event 고유키, delivery 재시도 메타데이터, 종료 생명주기
이중 소유, 확인 토큰 귀속, 인증 장애 circuit, 폭주 경계, 민감 본문 TTL,
부분체결 미체결 상태, 청산 terminal 판정과 수용 검증 공백을 발견했다.
계획에 정확한 인터페이스·스키마·red/green 테스트를 추가해 모두 수정했다.

재검토 중 발견된 청산 값 객체의 core→domain 역방향 의존 가능성도
`LiquidationTarget`·`LiquidationResult`를 `domain/trading/models.py`가
소유하도록 정정했다. 최종 재검토에서 네 관점 모두
Critical/Important 없음으로 승인했다.
