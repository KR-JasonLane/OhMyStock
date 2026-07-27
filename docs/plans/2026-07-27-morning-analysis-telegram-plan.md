# 아침 AI 분석 Telegram 요약 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** 매 거래일 성공한 아침 AI 분석을 Telegram으로 한 번 자동 전달하고,
`/analysis`와 `/digest`로 최근 보존 결과를 부수효과 없이 재조회한다.

**Architecture:** 분석 실행 경로는 변경하지 않는다. Telegram 쪽의 SQL read
model이 당일 성공 분석을 찾아 순수 presenter로 요약하고, 기존 durable
outbox·sender에 idempotent하게 물질화한다. 조회 명령도 같은 분석 presenter와
보존된 digest payload renderer를 재사용해 자동 알림과 수동 조회의 의미를
일치시킨다.

**Tech Stack:** Python 3.12, FastAPI lifespan, SQLAlchemy 2, PostgreSQL,
pytest/pytest-asyncio, 기존 Telegram Bot API adapter와 durable outbox

## Global Constraints

- 구현 전 기준 커밋은 이 계획 문서 커밋이다. 미커밋 Phase 7 Task 6 변경은 먼저
  별도 수용·리뷰·커밋하고 이 태스크 diff에 섞지 않는다.
- 실제 Telegram·키움 API·주문·분석 재실행은 비라이브 검증에서 호출하지 않는다.
- `domain/analysis/`, AI 프롬프트, 후보 선정, 유동성·갭·자금 방어선을 변경하지
  않는다.
- 자동 알림 idempotency key는
  `analysis-summary:{run_environment}:{analysis_run_id}` 형식을 사용한다.
- 자동 catch-up은 현재 KST 거래일에 성공한 분석으로 제한한다.
- `/analysis`와 `/digest`는 읽기 전용이며 confirmation·제어 intent·외부 adapter
  호출을 만들지 않는다.
- 분석 요약과 digest query 응답은 `sensitive=true`이며 기존 query/digest TTL과
  scrub 계약을 유지한다.
- `mock`은 `모의투자`, `real`은 `실전`으로 표시하고 그 밖의 환경은 fail-closed한다.
- 동적 AI 문자열은 Telegram parse mode가 없는 plain text로만 전달한다.
- 각 구현 Task 뒤 회고와 `$ohmystock-review-panel` 네 관점 리뷰를 수행한다.
  브로커·TR·주문 코드는 변경하지 않으므로 `broker-api-expert`는 사용하지 않는다.
- 각 Task 커밋 전 전체 메시지와 포함 파일을 사용자에게 제시하고 승인을 받는다.

---

## 파일 구조

```text
backend/app/domain/notifications/
  analysis_summary.py       # 신규: 허용목록 DTO, 분류, 요약 presenter
  digest.py                 # 수정: 저장 payload의 안전한 digest 재표시
  models.py                 # 수정: ANALYSIS, DIGEST command kind
  parsing.py                # 기존 enum 기반 파서 소비, 동작 변경 테스트
  commands.py               # 수정: 읽기 전용 report query port 라우팅
  presentation.py           # 수정: /help 두 명령

backend/app/store/
  notification_store.py     # 수정: 분석 read model, 자동 outbox, 최근 digest payload

backend/app/core/
  telegram_service.py       # 수정: 자동 요약 service, query lane 분류·조립

backend/app/
  main.py                   # 수정: store/service/command query port 조립

backend/tests/notifications/
  test_analysis_summary.py  # 신규: 순수 요약·분류·길이
  test_notification_store.py# 수정: SQL read/materialize/query
  test_commands.py          # 수정: /analysis, /digest query
  test_digest.py            # 수정: 저장 payload renderer
  test_service.py           # 수정: poll loop·lifespan 조립

docs/retrospectives/
  2026-07-27-morning-analysis-telegram.md
```

---

### Task 1: 순수 분석 요약 모델과 presenter

**Files:**

- Create: `backend/app/domain/notifications/analysis_summary.py`
- Create: `backend/tests/notifications/test_analysis_summary.py`
- Create: `docs/retrospectives/2026-07-27-morning-analysis-telegram-task1.md`

**Interfaces:**

- Produces:

```python
@dataclass(frozen=True)
class AnalysisVerdictSummary:
    symbol: str
    name: str | None
    verdict: str
    confidence: float
    reasons: tuple[str, ...]
    risk_flags: tuple[str, ...]
    picked: bool
    pick_rank: int | None

@dataclass(frozen=True)
class MorningAnalysisSummary:
    run_id: int
    run_environment: str
    regime: str
    market_summary: str
    max_picks_advice: int
    score_reference_date: date
    started_at: datetime
    finished_at: datetime
    verdicts: tuple[AnalysisVerdictSummary, ...]
    corrupted_rows: int = 0

    @property
    def idempotency_key(self) -> str: ...

def render_analysis_summary(summary: MorningAnalysisSummary) -> str: ...
def render_analysis_parts(
    summary: MorningAnalysisSummary,
) -> tuple[str, ...]: ...
```

- `render_analysis_parts`는 기존 `render_parts`를 호출하고 correlation id로
  `analysis-summary-{environment}-{run_id}`를 사용한다.
- 최종 후보는 `picked=true`와 `pick_rank` 오름차순, 차순위 승인은
  `approve && !picked`를 `(-confidence, symbol)`로 정렬한다.
- 최종 후보별 reasons/risk_flags는 각각 2개, 차순위는 3종목까지만 표시한다.

- [ ] **Step 1: 최종 후보와 차순위 분류 RED 테스트**

실제 DTO를 만들어 다음 literal을 검증한다.

```python
def test_analysis_summary는_최종후보와_차순위승인을_구분한다():
    text = render_analysis_summary(_summary(
        max_picks_advice=1,
        verdicts=(
            _verdict("007160", "사조산업", "approve", 0.65, True, 1),
            _verdict("475150", "SK이터닉스", "approve", 0.65, False, None),
            _verdict("001790", "대한제당", "reject", 0.90, False, None),
        ),
    ))
    assert "🎯 최종 후보" in text
    assert "007160 · 사조산업" in text
    assert "📋 차순위 승인" in text
    assert "475150 · SK이터닉스" in text
    assert "검토 결과  승인 2 · 거절 1" in text
```

- [ ] **Step 2: RED 확인**

Run:

```bash
cd backend
uv run pytest tests/notifications/test_analysis_summary.py -q
```

Expected: `ModuleNotFoundError:
app.domain.notifications.analysis_summary`.

- [ ] **Step 3: DTO 검증과 결정론적 분류 최소 구현**

다음 입력을 생성 시점에 거부한다.

- `run_id < 1`
- 환경이 `mock|real` 이외
- timezone-naive timestamp
- verdict가 `approve|reject` 이외
- confidence가 0~1 밖
- picked와 pick_rank의 불일치
- 비어 있거나 12자를 넘는 symbol

종목명이 없으면 symbol만 표시한다. 환경 라벨은 exact mapping을 사용한다.

```python
_ENVIRONMENT_LABEL = {"mock": "모의투자", "real": "🚨 실전"}
```

- [ ] **Step 4: 후보 없음·상한·plain-text 회귀를 추가해 RED 확인**

테스트는 다음을 각각 독립 assertion으로 고정한다.

- 후보 없음: `오늘 최종 진입 후보가 없습니다.`
- reasons/risk_flags 각 3개 입력 시 앞 2개만 표시
- 차순위 4개 입력 시 3개만 표시하고 `차순위 승인 전체 4종목` 표시
- `<b>매수</b>`가 그대로 출력되고 parse mode가 생기지 않음
- `render_analysis_parts`의 모든 part 길이가 4096 이하
- `corrupted_rows=2`이면 `확인할 수 없는 분석 행  2건`

- [ ] **Step 5: GREEN과 관련 회귀 확인**

Run:

```bash
cd backend
uv run pytest tests/notifications/test_analysis_summary.py \
  tests/notifications/test_formatting.py -q
```

Expected: PASS.

- [ ] **Step 6: Task 1 회고·리뷰**

회고에는 분류 규칙, 표시 상한, fail-closed 환경, plain-text 경계를 기록한다.
네 관점 리뷰 후 Critical·Important를 수정하고 해당 관점 재검토를 받는다.

- [ ] **Step 7: 커밋 승인**

제안 메시지:

```text
feat(telegram): present morning analysis summaries
```

---

### Task 2: 분석 SQL read model과 durable 자동 outbox

**Files:**

- Modify: `backend/app/store/notification_store.py`
- Modify: `backend/tests/notifications/test_notification_store.py`
- Modify: `backend/tests/notifications/test_analysis_summary.py`
- Create: `docs/retrospectives/2026-07-27-morning-analysis-telegram-task2.md`

**Interfaces:**

- Produces:

```python
class AnalysisSummaryRunStore:
    def __init__(
        self,
        engine: Engine,
        run_environment: str,
        now: Callable[[], datetime] | None = None,
    ) -> None: ...

    def pending_succeeded_today(
        self,
        generated_run_ids: Collection[int],
        limit: int = 10,
    ) -> tuple[MorningAnalysisSummary, ...]: ...

    def latest_analysis(self) -> MorningAnalysisSummary | None: ...

class NotificationStore:
    def generated_analysis_run_ids(
        self,
        run_environment: str,
    ) -> tuple[int, ...]: ...

    def materialize_analysis_summary(
        self,
        summary: MorningAnalysisSummary,
        bodies: Sequence[str],
        *,
        occurred_at: datetime,
    ) -> MaterializedDigest: ...
```

`MaterializedDigest`는 일반화해 다음 이름으로 바꾼다.

```python
@dataclass(frozen=True)
class MaterializedNotification:
    outbox_id: int
    created: bool
    priority: NotificationPriority
```

기존 digest 호출부와 테스트도 새 이름으로 기계적으로 변경한다.

- [ ] **Step 1: 실제 SQLite 모델을 쓰는 pending read RED 테스트**

`Base.metadata.create_all` DB에 KST 전일/당일의 failed/succeeded analysis와
verdict/instrument를 넣는다. `pending_succeeded_today({2})`가 당일 succeeded 중
run 2를 제외하고 오래된 순서·limit으로 반환하는지 literal run id로 검증한다.

- [ ] **Step 2: RED 확인**

Run:

```bash
cd backend
uv run pytest \
  tests/notifications/test_notification_store.py \
  tests/notifications/test_analysis_summary.py -q
```

Expected: import 또는 missing attribute FAIL.

- [ ] **Step 3: read model 최소 구현**

- KST 당일 범위는 `coarse_utc_bounds`로 SQL을 줄이고 `within_kst_day`로 확정한다.
- `AnalysisRunRow.status == "succeeded"`와 `finished_at IS NOT NULL`을 요구한다.
- `ScoreRunRow.reference_date`를 읽어 `score_reference_date`를 채운다.
- `AnalysisVerdictRow` 전체를 읽고 `InstrumentRow.symbol`로 이름을 left lookup한다.
- reasons/risk_flags JSON은 JSON string 배열만 허용한다. 손상 행은 빈 tuple로
  격리하고 `corrupted_rows`를 증가시킨다.
- 조회는 broker·network를 호출하지 않는다.

- [ ] **Step 4: materialize 원자성·idempotency RED 테스트**

같은 `MorningAnalysisSummary`를 두 번 materialize하고 다음을 검증한다.

```python
assert first.created is True
assert second.created is False
assert store.count_outbox() == 1
assert store.outbox_payload(first.outbox_id)["analysis_run_id"] == 7
assert len(store.delivery_bodies(first.outbox_id)) == len(bodies)
```

outbox 값은 다음 exact 계약을 사용한다.

```text
kind=analysis_summary
priority=NORMAL
sensitive=true
retention_kind=digest
purge_at=created_at+24h
```

- [ ] **Step 5: materialize 구현과 GREEN**

payload에는 presenter 문자열을 넣지 않고 다음 허용목록만 넣는다.

```python
{
    "version": 1,
    "analysis_run_id": summary.run_id,
    "run_environment": summary.run_environment,
    "score_reference_date": summary.score_reference_date.isoformat(),
}
```

`_checked_delivery_bodies`, `_insert_outbox`, `_add_delivery_rows`를 재사용해
outbox와 parts를 한 트랜잭션에 생성한다.

Run:

```bash
cd backend
uv run pytest tests/notifications/test_notification_store.py \
  tests/notifications/test_analysis_summary.py \
  tests/notifications/test_digest.py -q
```

Expected: PASS.

- [ ] **Step 6: Task 2 회고·리뷰·커밋 승인**

SQL 상한, 손상 격리, idempotency, 24시간 scrub을 회고에 기록한다. 리뷰 후
제안 메시지는 다음과 같다.

```text
feat(telegram): persist morning analysis notifications
```

---

### Task 3: 자동 분석 요약 서비스와 Telegram 수명주기

**Files:**

- Modify: `backend/app/core/telegram_service.py`
- Modify: `backend/app/main.py`
- Modify: `backend/tests/notifications/test_service.py`
- Modify: `backend/tests/test_main.py`
- Create: `docs/retrospectives/2026-07-27-morning-analysis-telegram-task3.md`

**Interfaces:**

- Consumes:
  `AnalysisSummaryRunStore.pending_succeeded_today`,
  `NotificationStore.generated_analysis_run_ids`,
  `NotificationStore.materialize_analysis_summary`,
  `render_analysis_parts`.
- Produces:

```python
class AnalysisSummaryService:
    def __init__(
        self,
        runs: AnalysisSummaryRunStore,
        store: NotificationStore,
        *,
        run_environment: str,
        now: Callable[[], datetime] | None = None,
        batch_size: int = 10,
    ) -> None: ...

    async def run_once(self) -> int: ...
```

- [ ] **Step 1: 자동 materialize RED 테스트**

fake가 당일 summary 2개를 반환하게 하고 `run_once()`가 오래된 순서로 두
outbox를 만들며 created 수 2를 반환하는지 검증한다. 같은 fake를 재호출하면
generated ids 때문에 0을 반환해야 한다.

- [ ] **Step 2: 장애 격리 RED 테스트**

- read model 예외는 분석 DB를 변경하지 않고 service tick을 실패시킨다.
- 한 summary materialize 뒤 두 번째가 실패하면 첫 outbox는 유지되고 다음 tick이
  나머지를 복구한다.
- Telegram adapter는 `run_once`에서 호출되지 않는다.

- [ ] **Step 3: RED 확인 후 최소 구현**

Run:

```bash
cd backend
uv run pytest tests/notifications/test_service.py \
  -k analysis_summary -q
```

Expected: missing `AnalysisSummaryService`.

`run_once`는 모든 동기 store 호출을 `asyncio.to_thread`로 보낸다.

- [ ] **Step 4: composite loop 조립**

기존 maintenance/digest composite tick에 `analysis_summary` operation을 추가한다.
독립 무한 loop를 새로 만들지 않는다. snapshot에는 본문 없이 다음만 노출한다.

```python
{
    "state": "running|dead",
    "last_created": int,
    "backoff_reason": str | None,
}
```

한 component 실패가 sender·commands·poller를 취소하지 않는 기존 loop failure
격리를 유지한다.

- [ ] **Step 5: main 조립과 lifespan 회귀**

`telegram_enabled`일 때만 `AnalysisSummaryRunStore`와
`AnalysisSummaryService`를 만든다. configured `settings.run_environment`를
store·service에 동일하게 전달한다. 종료 순서는 기존 TelegramService가 소유하고
별도 shutdown hook을 추가하지 않는다.

Run:

```bash
cd backend
uv run pytest tests/notifications/test_service.py tests/test_main.py -q
```

Expected: PASS, 실제 Telegram·broker 호출 0.

- [ ] **Step 6: Task 3 회고·리뷰·커밋 승인**

재기동 catch-up, 동일 tick 중복, component 장애 격리를 기록한다.

```text
feat(telegram): deliver morning analysis summaries
```

---

### Task 4: `/analysis` 조회 명령

**Files:**

- Modify: `backend/app/domain/notifications/models.py`
- Modify: `backend/app/domain/notifications/commands.py`
- Modify: `backend/app/domain/notifications/presentation.py`
- Modify: `backend/app/core/telegram_service.py`
- Modify: `backend/app/main.py`
- Modify: `backend/tests/notifications/test_parsing.py`
- Modify: `backend/tests/notifications/test_commands.py`
- Modify: `backend/tests/notifications/test_presentation.py`
- Create: `docs/retrospectives/2026-07-27-morning-analysis-telegram-task4.md`

**Interfaces:**

- Add `CommandKind.ANALYSIS = "analysis"`.
- Add a domain port, implemented by `AnalysisSummaryRunStore`:

```python
class AnalysisReportQueryPort(Protocol):
    def latest_analysis(self) -> MorningAnalysisSummary | None: ...
```

- `CommandProcessor.__init__`에 keyword-only
  `analysis_reports: AnalysisReportQueryPort`를 추가한다.

- [ ] **Step 1: parser/query-lane RED 테스트**

```python
def test_analysis는_인자없는_조회명령이다():
    assert parse_command("/analysis").kind is CommandKind.ANALYSIS
    with pytest.raises(InvalidCommand):
        parse_command("/analysis now")
```

InboxPoller와 CommandDispatcher의 query allowlist가 `analysis`를 포함하고 control
lane에는 포함하지 않는지 실제 pending inbox 처리 결과로 검증한다.

- [ ] **Step 2: command response RED 테스트**

`analysis_reports.latest_analysis()`가 summary를 반환할 때 `response_text`가
`render_analysis_summary(summary)`와 같고 다음을 검증한다.

```python
assert result.kind == "analysis"
assert result.outbox_sensitive is True
assert control.calls == []
```

None이면 exact text는 다음이다.

```text
🧠 최근 AI 분석

조회 가능한 AI 분석이 없습니다.
```

- [ ] **Step 3: 최소 구현과 재조정 경로**

`process_claimed`의 terminal read-only rebuild allowlist와
`_reconcile_non_liquidation` 성공 allowlist에도 `ANALYSIS`를 추가한다.
`_call_control`의 이름은 `_call_handler`로 바꾸고, ANALYSIS 분기만 reports port를
`asyncio.to_thread`로 호출한다. 기존 control 명령 분기 의미는 변경하지 않는다.

- [ ] **Step 4: `/help` 가독성 테스트와 구현**

`TelegramCommandPresenter.help()`의 조회 절에 exact line을 추가한다.

```text
/analysis   최근 AI 분석
```

기존 `/status`, `/account`, `/positions`가 그대로 한 번씩 표시되는지도 함께
검증한다.

- [ ] **Step 5: GREEN**

Run:

```bash
cd backend
uv run pytest tests/notifications/test_parsing.py \
  tests/notifications/test_commands.py \
  tests/notifications/test_presentation.py \
  tests/notifications/test_service.py -q
```

Expected: PASS.

- [ ] **Step 6: Task 4 회고·리뷰·커밋 승인**

읽기 전용 lane, no-AI/no-broker 부수효과, query TTL을 기록한다.

```text
feat(telegram): query latest morning analysis
```

---

### Task 5: `/digest` 보존 결과 조회

**Files:**

- Modify: `backend/app/domain/notifications/models.py`
- Modify: `backend/app/domain/notifications/digest.py`
- Modify: `backend/app/domain/notifications/commands.py`
- Modify: `backend/app/domain/notifications/presentation.py`
- Modify: `backend/app/core/telegram_service.py`
- Modify: `backend/app/store/notification_store.py`
- Modify: `backend/tests/notifications/test_digest.py`
- Modify: `backend/tests/notifications/test_notification_store.py`
- Modify: `backend/tests/notifications/test_commands.py`
- Modify: `backend/tests/notifications/test_presentation.py`
- Create: `docs/retrospectives/2026-07-27-morning-analysis-telegram-task5.md`

**Interfaces:**

- Add `CommandKind.DIGEST = "digest"`.
- Add a separate domain port:

```python
class DigestReportQueryPort(Protocol):
    def latest_digest_payload(self) -> Mapping[str, object] | None: ...
```

- `CommandProcessor.__init__`에 keyword-only
  `digest_reports: DigestReportQueryPort`를 추가한다. Task 4의
  `analysis_reports`와 합치지 않아 각 read model의 책임과 테스트 double을
  분리한다.
- Produce:

```python
def render_retained_digest(payload: Mapping[str, object]) -> str: ...

class NotificationStore:
    def latest_retained_digest_payload(
        self,
        run_environment: str,
        *,
        now: datetime,
    ) -> dict[str, object] | None: ...

class DigestReportStore:
    def __init__(
        self,
        notifications: NotificationStore,
        run_environment: str,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None: ...

    def latest_digest_payload(self) -> dict[str, object] | None: ...
```

- [ ] **Step 1: 저장 payload 재표시 RED 테스트**

기존 `Digest.payload` literal fixture를 `render_retained_digest`에 넣고
`Digest.body`와 동일한 본문인지 검증한다. schema version, 환경, 날짜, section,
account 필드가 누락·잘못된 타입이면 `ValueError`여야 한다.

- [ ] **Step 2: 최신 retained 조회 RED 테스트**

SQLite에 다음을 삽입한다.

- mock digest sent, `purge_at > now`, body/payload 존재
- 더 최신 mock digest지만 scrub돼 payload/body 없음
- real digest retained

mock 조회는 가장 최근의 **본문·payload가 모두 보존된** mock digest만 반환해야
한다. 조회 전후 outbox status/purge_at/body가 변하지 않는지도 비교한다.

- [ ] **Step 3: store와 renderer 최소 구현**

store filter는 다음을 모두 요구한다.

```text
kind='digest'
idempotency_key LIKE 'digest:{environment}:%'
sensitive=true
purge_at > now
payload IS NOT NULL
최소 한 delivery body IS NOT NULL
```

payload를 `json.loads`한 결과가 object가 아니면 해당 row를 건너뛰고 이전 retained
row를 찾되 100행 상한을 둔다.

- [ ] **Step 4: command RED와 구현**

`/digest`를 query allowlist와 read-only terminal/reconciliation allowlist에
추가한다. retained payload가 있으면 `render_retained_digest`, 없으면 다음 exact
text를 반환한다.

```text
📋 최근 거래 다이제스트

조회 가능한 최근 거래 다이제스트가 없습니다.
```

응답은 `outbox_sensitive=True`이고 account/broker adapter 호출은 0이어야 한다.

- [ ] **Step 5: `/help`와 전체 관련 GREEN**

추가 line:

```text
/digest     최근 거래 다이제스트
```

Run:

```bash
cd backend
uv run pytest tests/notifications/test_digest.py \
  tests/notifications/test_notification_store.py \
  tests/notifications/test_parsing.py \
  tests/notifications/test_commands.py \
  tests/notifications/test_presentation.py \
  tests/notifications/test_service.py -q
```

Expected: PASS.

- [ ] **Step 6: Task 5 회고·리뷰·커밋 승인**

과거 값을 현재 계좌로 재계산하지 않는 이유, TTL 뒤 unavailable, 환경 격리를
회고에 기록한다.

```text
feat(telegram): query retained trading digest
```

---

### Task 6: 전체 회귀, 운영 문서, 모의 Telegram 수용 준비

**Files:**

- Modify: `docs/runbooks/telegram.md`
- Modify: `docs/STATUS.md`
- Create: `docs/retrospectives/2026-07-27-morning-analysis-telegram.md`

**Interfaces:**

- 자동 알림: 성공 분석당 한 번
- 조회: `/analysis`, `/digest`
- 기존 명령과 16:10 자동 digest 동작 유지

- [ ] **Step 1: 비라이브 전체 회귀**

Run:

```bash
cd backend
uv run pytest
```

Expected: 기존 정책대로 live 11건 deselected, failure 0. 기존 Starlette warning
외 새 warning 0.

- [ ] **Step 2: 정적·마이그레이션 검증**

DB schema 변경이 없어 Alembic revision은 추가하지 않는다.

Run:

```bash
cd backend
uv run python -m compileall -q app
uv run alembic heads
git diff --check
```

Expected: compile PASS, 단일 기존 head, diff 오류 없음.

- [ ] **Step 3: 합성 end-to-end**

실제 Telegram adapter 대신 fake sender, 실제 SQLite store/service/dispatcher를
사용해 다음 흐름을 한 테스트에서 검증한다.

1. succeeded analysis 저장
2. automatic summary outbox 1개 생성
3. sender가 고정 body 전달
4. `/analysis` inbox 처리와 query response
5. retained digest 저장
6. `/digest` inbox 처리와 당시 본문 재표시
7. 같은 tick·재기동 service로 duplicate 0

실패시키는 production mutation은 각각 다음이다.

- idempotency prefix 제거 → outbox count assertion 실패
- picked/alternate 분류 반전 → body literal 실패
- `/digest`에서 account 재조회 → fake control call assertion 실패
- environment filter 제거 → real/mock fixture assertion 실패

- [ ] **Step 4: runbook과 STATUS**

runbook에 다음을 추가한다.

- 자동 알림 예상 시점: 08:20 분석 성공 직후
- `/analysis`, `/digest` 의미와 TTL
- 자동 알림 미도착 시 `/status`와 sender/outbox 확인
- 분석·주문을 수동 재실행하지 않는 복기 순서
- 실제 수용은 저장된 합성 run 또는 다음 정상 아침 분석으로만 수행

`STATUS.md`에는 구현 커밋, 테스트 수, 실제 Telegram 미호출 여부, 다음 수용
체크포인트를 기록한다.

- [ ] **Step 5: 최종 패널**

전체 feature diff를 `senior-developer`, `senior-trader`,
`architecture-expert`, `security-expert`가 독립 검토한다. Critical·Important를
수정하고 해당 관점 재검토 뒤 전원 승인받는다.

- [ ] **Step 6: 실제 Telegram 수용 승인 게이트**

사용자 별도 승인 뒤 모의환경에서만 다음을 확인한다.

- `/analysis` 1회
- `/digest` 1회
- 다음 정상 08:20 분석 자동 알림 1회
- 동일 analysis run 중복 알림 없음

분석 재실행, 주문, pause/stop/resume/liquidate 명령은 호출하지 않는다.

- [ ] **Step 7: 최종 커밋 승인**

제안 메시지:

```text
docs(telegram): complete morning analysis notifications
```
