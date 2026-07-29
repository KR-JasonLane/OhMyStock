# Telegram 다이제스트 거래 경고 요약 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** DB에 영속된 거래 경고를 원문 노출 없이 구조화해 장 마감 Telegram 다이제스트에 최대 5건의 한국어 요약으로 표시한다.

**Architecture:** domain은 허용된 경고 코드와 제한된 scalar만 가진 `DigestTradeNotice`를 검증·표시하고, store adapter는 `trade_runs.warnings` 자유 텍스트를 정규식 허용목록으로 변환한다. 기존 payload version 1에는 선택 필드를 추가해 과거 payload를 계속 읽으며, 거래 엔진·DB schema·기존 경고 저장 방식은 변경하지 않는다.

**Tech Stack:** Python 3.12, dataclass, regular expressions, SQLAlchemy, pytest, 기존 Telegram plain-text formatter

## Global Constraints

- 승인 사양은 `docs/specs/2026-07-29-readable-digest-trade-warnings-design.md`를 기준으로 한다.
- 거래 경고 DB 원문을 Telegram payload·본문·새 테스트 fixture에 그대로 복사하지 않는다.
- 다이제스트에는 최대 5건을 표시하고 고유 경고가 더 많으면 `외 N건`으로 축약한다.
- 알 수 없는 경고는 원문 대신 `일부 거래 상태 확인 필요`로 통합한다.
- `trade_runs.warnings`, DB schema, 거래 로직, 진입 기준과 주문 경로를 변경하지 않는다.
- `Digest.payload` version 1과 기존 delivery part 재전송 계약을 유지한다.
- 실제 Telegram, 키움 API, 주문, 분석 재실행과 운영 DB를 검증에서 호출하지 않는다.

---

## 파일 구조

- `backend/app/domain/notifications/digest.py`
  - `DigestTradeNotice`의 허용 code·symbol·금액 검증을 소유한다.
  - `DigestSection`의 최대 5개 notice와 전체 `notice_count`를 검증한다.
  - payload 선택 필드와 과거 version 1 호환 parsing을 소유한다.
  - pipeline 상태와 notice code를 결합해 한국어 거래 경고 절을 표시한다.
- `backend/app/store/digest_trade_notices.py`
  - DB 자유 텍스트 한 줄을 허용된 `DigestTradeNotice`로 변환하는 순수 adapter
    정규화 함수만 소유한다.
  - 원문을 반환하거나 로깅하지 않는다.
- `backend/app/store/notification_store.py`
  - 해당 거래일·환경의 trade run 경고를 발생 순서로 읽고 정규화한다.
  - 중복 제거, 최대 5개 보존, 전체 고유 경고 수 집계를 수행한다.
- `backend/tests/notifications/test_digest.py`
  - DTO·payload 호환·한국어 표시·최대 건수·복구 여부를 검증한다.
- `backend/tests/notifications/test_digest_trade_notices.py`
  - 알려진 DB 경고 패턴과 알 수 없는 원문의 fail-closed 정규화를 검증한다.
- `backend/tests/notifications/test_commands.py`
  - `/digest`의 기존 delivery part 원문 재전송을 계속 검증한다.
- `docs/retrospectives/2026-07-29-readable-digest-trade-warnings.md`
  - 요청, 설계 판단, 정확한 변경 위치, TDD·리뷰·검증 결과와 수용 범위를 기록한다.
- `docs/STATUS.md`
  - 완료 결과와 다음 정상 16:10 읽기 전용 수용 체크포인트를 갱신한다.

---

### Task 1: 안전한 경고 DTO·payload 호환·Telegram presenter

**Files:**
- Modify: `backend/app/domain/notifications/digest.py`
- Modify: `backend/tests/notifications/test_digest.py`
- Modify: `backend/tests/notifications/test_commands.py`
- Create: `docs/retrospectives/2026-07-29-readable-digest-trade-warnings.md`

**Interfaces:**
- Produces: `DigestTradeNotice(code: str, symbol: str | None = None, observed_krw: int | None = None, threshold_krw: int | None = None)`
- Produces: `DigestSection(..., notices: tuple[DigestTradeNotice, ...] = (), notice_count: int = 0)`
- Preserves: `Digest.payload["version"] == 1`, `render_retained_digest(payload) -> str`, `Digest.bodies`
- Consumes in Task 2: store adapter가 생성한 `DigestTradeNotice` tuple과 전체 고유 경고 수

- [ ] **Step 1: DTO 검증의 실패하는 테스트 작성**

  `backend/tests/notifications/test_digest.py` import에 `DigestTradeNotice`를
  추가하고 다음 계약을 작성한다.

  ```python
  @pytest.mark.parametrize(
      "notice",
      [
          DigestTradeNotice("liquidity", "003960", 252_557_535, 1_000_000_000),
          DigestTradeNotice("analysis_wait"),
          DigestTradeNotice("analysis_empty"),
          DigestTradeNotice("gap_guard", "005930"),
          DigestTradeNotice("already_held", "005930"),
          DigestTradeNotice("reentry_cooldown", "005930"),
          DigestTradeNotice("capacity", "005930"),
          DigestTradeNotice("missing_context", "005930"),
          DigestTradeNotice("missing_price", "005930"),
          DigestTradeNotice("requote_fallback", "005930"),
          DigestTradeNotice("quote_unstable", "005930"),
          DigestTradeNotice("order_attention", "005930"),
          DigestTradeNotice("unknown"),
      ],
  )
  def test_digest_trade_notice는_허용된구조만_받는다(notice):
      assert notice.code


  @pytest.mark.parametrize(
      "kwargs",
      [
          {"code": "raw_internal_text"},
          {"code": "liquidity", "symbol": "003960;secret"},
          {"code": "liquidity", "symbol": "003960", "observed_krw": -1,
           "threshold_krw": 1_000_000_000},
          {"code": "liquidity", "symbol": "003960", "observed_krw": 1},
          {"code": "gap_guard", "observed_krw": 1},
      ],
  )
  def test_digest_trade_notice는_손상되거나_불필요한값을_거부한다(kwargs):
      with pytest.raises(ValueError):
          DigestTradeNotice(**kwargs)
  ```

  `DigestSection`에 notices 6개, `notice_count < len(notices)`, 음수 count를
  넣으면 `ValueError`가 발생하는 테스트도 추가한다.

- [ ] **Step 2: DTO 테스트가 import 또는 생성 실패하는지 확인**

  Run:

  ```bash
  cd backend
  uv run pytest tests/notifications/test_digest.py -q
  ```

  Expected: `DigestTradeNotice`가 없어 collection 단계에서 FAIL하거나 새 DTO
  검증 테스트가 FAIL한다.

- [ ] **Step 3: 최소 DTO와 section 불변조건 구현**

  `backend/app/domain/notifications/digest.py`에 다음 허용 code와 dataclass를
  추가한다.

  ```python
  _DIGEST_NOTICE_CODES = frozenset({
      "analysis_wait", "analysis_empty", "liquidity", "gap_guard",
      "already_held", "reentry_cooldown", "capacity", "missing_context",
      "missing_price", "requote_fallback", "quote_unstable",
      "order_attention", "unknown",
  })
  _SYMBOL_REQUIRED_NOTICE_CODES = frozenset({
      "liquidity", "gap_guard", "already_held", "reentry_cooldown",
      "missing_context", "missing_price", "requote_fallback",
  })
  _SYMBOL_OPTIONAL_NOTICE_CODES = frozenset({
      "capacity", "quote_unstable", "order_attention",
  })


  @dataclass(frozen=True)
  class DigestTradeNotice:
      code: str
      symbol: str | None = None
      observed_krw: int | None = None
      threshold_krw: int | None = None

      def __post_init__(self) -> None:
          if self.code not in _DIGEST_NOTICE_CODES:
              raise ValueError("unsupported digest trade notice code")
          if self.code in _SYMBOL_REQUIRED_NOTICE_CODES:
              if self.symbol is None or re.fullmatch(r"[A-Z0-9]{1,12}", self.symbol) is None:
                  raise ValueError("digest trade notice requires a safe symbol")
          elif self.code in _SYMBOL_OPTIONAL_NOTICE_CODES:
              if self.symbol is not None and re.fullmatch(
                  r"[A-Z0-9]{1,12}", self.symbol
              ) is None:
                  raise ValueError("digest trade notice symbol must be safe")
          elif self.symbol is not None:
              raise ValueError("digest trade notice does not accept a symbol")
          amounts = (self.observed_krw, self.threshold_krw)
          if self.code == "liquidity":
              if any(type(value) is not int or value < 0 for value in amounts):
                  raise ValueError("liquidity notice requires nonnegative amounts")
          elif any(value is not None for value in amounts):
              raise ValueError("digest trade notice amounts are only for liquidity")
  ```

  `DigestSection`에 `notices`와 `notice_count`를 추가하고 다음을 강제한다.

  - notices는 tuple로 고정
  - 길이 5 이하
  - `notice_count`는 bool이 아닌 0 이상 int
  - `notice_count >= len(notices)`
  - notices의 모든 원소가 `DigestTradeNotice`

- [ ] **Step 4: DTO와 기존 digest 단위 테스트 통과 확인**

  Run:

  ```bash
  cd backend
  uv run pytest tests/notifications/test_digest.py -q
  ```

  Expected: PASS. 기존 `DigestSection` positional 생성은 새 필드 기본값으로
  호환된다.

- [ ] **Step 5: payload 호환의 실패하는 테스트 작성**

  `backend/tests/notifications/test_digest.py`에 다음을 추가한다.

  ```python
  def test_digest_payload은_구조화된거래경고만_보존한다():
      digest = _readable_digest(
          trading_notices=(
              DigestTradeNotice(
                  "liquidity", "003960", 252_557_535, 1_000_000_000
              ),
          ),
          trading_notice_count=1,
      )

      assert digest.payload["trading"]["notices"] == [{
          "code": "liquidity",
          "symbol": "003960",
          "observed_krw": 252_557_535,
          "threshold_krw": 1_000_000_000,
      }]
      assert digest.payload["trading"]["notice_count"] == 1
      assert "entry dropped" not in str(digest.payload)


  def test_기존_v1_payload은_경고선택필드없이_계속표시된다():
      payload = deepcopy(_retained_digest().payload)
      payload["pipeline"].pop("notices", None)
      payload["pipeline"].pop("notice_count", None)
      payload["trading"].pop("notices", None)
      payload["trading"].pop("notice_count", None)

      assert render_retained_digest(payload)
  ```

  손상된 notice code·symbol·금액·6개 notice·count 불일치 retained payload는
  모두 `ValueError`로 거부하는 매개변수 테스트를 추가한다.

- [ ] **Step 6: payload 테스트가 선택 필드 부재로 실패하는지 확인**

  Run:

  ```bash
  cd backend
  uv run pytest tests/notifications/test_digest.py -q
  ```

  Expected: 새 payload에 `notices`가 없어 FAIL한다.

- [ ] **Step 7: version 1 선택 필드 serializer와 parser 구현**

  `_section_payload()`가 다음 두 필드를 추가한다.

  ```python
  "notices": [
      {
          "code": notice.code,
          "symbol": notice.symbol,
          "observed_krw": notice.observed_krw,
          "threshold_krw": notice.threshold_krw,
      }
      for notice in section.notices
  ],
  "notice_count": section.notice_count,
  ```

  `_retained_section()`은 두 key가 모두 없으면 `(), 0`으로 읽고, 둘 중 하나만
  있거나 형식이 손상되면 `ValueError`를 발생시킨다. 각 notice object는
  정확히 네 key를 요구해 `DigestTradeNotice`로 재구성한다.

- [ ] **Step 8: Telegram 표시의 실패하는 exact 테스트 작성**

  다음 세 경우를 추가한다.

  ```python
  def test_digest본문은_거래경고를_최대5건_한국어로_표시한다():
      digest = _readable_digest(
          trading_notices=(
              DigestTradeNotice("analysis_wait"),
              DigestTradeNotice(
                  "liquidity", "003960", 252_557_535, 1_000_000_000
              ),
              DigestTradeNotice("gap_guard", "005930"),
              DigestTradeNotice("already_held", "000660"),
              DigestTradeNotice("unknown"),
          ),
          trading_notice_count=7,
      )

      assert "⚠️ 오늘 발생한 거래 경고" in digest.body
      assert "- AI 분석 지연 후 정상 복구" in digest.body
      assert "- 003960 · 유동성 기준 미달 (2.53억 / 기준 10억)" in digest.body
      assert "- 005930 · 가격 변동폭 기준 초과" in digest.body
      assert "- 000660 · 이미 보유 중이라 진입하지 않음" in digest.body
      assert "- 일부 거래 상태 확인 필요" in digest.body
      assert "- 외 2건" in digest.body


  def test_digest본문은_분석지연이복구되지않으면_실패로표시한다():
      digest = _readable_digest(
          analysis_status="failed",
          trading_notices=(DigestTradeNotice("analysis_wait"),),
          trading_notice_count=1,
      )

      assert "- AI 분석 결과를 제때 사용하지 못함" in digest.body


  def test_digest본문은_거래경고가없으면_절을숨긴다():
      assert "오늘 발생한 거래 경고" not in _readable_digest().body
  ```

  `_readable_digest()` helper에 `analysis_status`, `trading_notices`,
  `trading_notice_count` keyword를 추가한다.

- [ ] **Step 9: 거래 경고 presenter 최소 구현**

  `_render_digest()`에서 자동매매 절 다음에 `_render_trade_notices(digest)`가
  반환한 non-empty 절을 삽입한다. 고정 매핑은 승인 사양 §5와 일치시킨다.

  분석 복구는 다음 조건일 때만 성공으로 표시한다.

  ```python
  analysis_recovered = (
      digest.pipeline.facts.get("analysis_status") == "succeeded"
      and _short_date(
          digest.pipeline.facts.get("analysis_score_reference_day")
      ) is not None
  )
  ```

  억 원 표시는 `Decimal(value) / Decimal(100_000_000)`을 사용하고 소수점
  둘째 자리까지 반올림한 뒤 불필요한 `.00`과 끝 0을 제거한다. float를
  사용하지 않는다.

- [ ] **Step 10: Task 1 관련 회귀 통과 확인**

  Run:

  ```bash
  cd backend
  uv run pytest \
    tests/notifications/test_digest.py \
    tests/notifications/test_commands.py \
    tests/notifications/test_service.py \
    tests/notifications/test_notification_store.py -q
  ```

  Expected: PASS. `/digest`는 기존 delivery part를 계속 우선 반환한다.

- [ ] **Step 11: Task 1 회고 작성·리뷰 패널·커밋 승인**

  회고 `docs/retrospectives/2026-07-29-readable-digest-trade-warnings.md`에
  Task 1의 요청, DTO·payload 선택 필드 판단, 변경 위치와 테스트 결과를
  기록한다.

  `$ohmystock-review-panel`로 `senior-developer`, `senior-trader`,
  `architecture-expert`, `security-expert`가 같은 diff를 독립 검토한다.
  broker·TR·주문·인증 코드는 변경하지 않으므로 `broker-api-expert`는
  호출하지 않는다. Critical/Important는 TDD 수정 후 해당 관점 재승인을
  받는다.

  사용자에게 다음 예상 커밋을 실제 diff에 맞게 정확히 제시하고 승인 뒤에만
  커밋한다.

  ```text
  feat(telegram): model digest trade warnings
  ```

  포함 파일:

  ```text
  backend/app/domain/notifications/digest.py
  backend/tests/notifications/test_digest.py
  backend/tests/notifications/test_commands.py
  docs/retrospectives/2026-07-29-readable-digest-trade-warnings.md
  ```

---

### Task 2: DB 경고 정규화와 다이제스트 read model 연결

**Files:**
- Create: `backend/app/store/digest_trade_notices.py`
- Create: `backend/tests/notifications/test_digest_trade_notices.py`
- Modify: `backend/app/store/notification_store.py`
- Modify: `backend/tests/notifications/test_digest.py`
- Modify: `docs/retrospectives/2026-07-29-readable-digest-trade-warnings.md`
- Modify: `docs/STATUS.md`

**Interfaces:**
- Consumes: Task 1의 `DigestTradeNotice`, `DigestSection.notices`,
  `DigestSection.notice_count`
- Produces: `normalize_trade_warning(line: str) -> DigestTradeNotice | None`
- Produces: `collect_trade_notices(warnings: Sequence[str | None]) -> tuple[tuple[DigestTradeNotice, ...], int]`
- Preserves: `DigestRunStore.trading_summary(trading_day) -> DigestSection`

- [ ] **Step 1: 정규화 허용목록의 실패하는 테스트 작성**

  `backend/tests/notifications/test_digest_trade_notices.py`에 원문 형태를
  최소 합성 fixture로 작성한다.

  ```python
  def test_유동성경고는_종목과금액만_구조화한다():
      notice = normalize_trade_warning(
          "003960: entry dropped — liquidity: avg value "
          "252,557,535 < 1,000,000,000"
      )

      assert notice == DigestTradeNotice(
          "liquidity", "003960", 252_557_535, 1_000_000_000
      )


  @pytest.mark.parametrize(
      ("line", "expected"),
      [
          ("no analysis result yet — will retry within entry window",
           DigestTradeNotice("analysis_wait")),
          ("analysis signal date mismatch (signal DATE, expected DATE) — "
           "stale signal; will retry within entry window",
           DigestTradeNotice("analysis_wait")),
          ("analysis picks empty — no entries today",
           DigestTradeNotice("analysis_empty")),
          ("005930: entry dropped — already held (§6-3.3)",
           DigestTradeNotice("already_held", "005930")),
          ("005930: entry dropped — reentry cooldown (recently closed)",
           DigestTradeNotice("reentry_cooldown", "005930")),
          ("005930: entry dropped — free slots exhausted (0)",
           DigestTradeNotice("capacity", "005930")),
          ("005930: pick missing context/quote",
           DigestTradeNotice("missing_context", "005930")),
          ("005930: entry dropped — price missing (signal 0, current 0)",
           DigestTradeNotice("missing_price", "005930")),
          ("005930: entry dropped — gap guard: current VALUE vs signal VALUE",
           DigestTradeNotice("gap_guard", "005930")),
          ("005930: pre-entry requote failed — using batch snapshot",
           DigestTradeNotice("requote_fallback", "005930")),
      ],
  )
  def test_알려진경고는_허용코드만_반환한다(line, expected):
      assert normalize_trade_warning(line) == expected
  ```

  테스트 fixture의 `DATE`와 `VALUE`는 정규식 분기만 검증하는 합성 값이며
  운영 원문을 그대로 보존하지 않는다.

- [ ] **Step 2: 알 수 없는 원문과 민감정보 비노출 테스트 작성**

  ```python
  def test_알수없는경고는_원문없이_unknown으로_축약한다():
      secret = "unexpected warning TOKEN_RAW_SECRET"

      notice = normalize_trade_warning(secret)

      assert notice == DigestTradeNotice("unknown")
      assert secret not in repr(notice)


  def test_손상된유동성숫자는_unknown으로_축약한다():
      assert normalize_trade_warning(
          "003960: entry dropped — liquidity: avg value -1 < secret"
      ) == DigestTradeNotice("unknown")
  ```

  빈 줄은 `None`을 반환하도록 signature를
  `normalize_trade_warning(line: str) -> DigestTradeNotice | None`으로
  확정한다.

- [ ] **Step 3: 정규화 테스트가 모듈 부재로 실패하는지 확인**

  Run:

  ```bash
  cd backend
  uv run pytest tests/notifications/test_digest_trade_notices.py -q
  ```

  Expected: module import failure로 FAIL한다.

- [ ] **Step 4: 순수 정규화 모듈 최소 구현**

  `backend/app/store/digest_trade_notices.py`는 다음 원칙으로 구현한다.

  - module-level precompiled regex 사용
  - 정확한 고정 prefix·suffix와 안전한 symbol만 허용
  - 유동성 쉼표 정수는 제거 후 `int` 변환하고 0 이상인지 DTO가 검증
  - gap, held, cooldown, capacity, missing context/price, requote,
    quote 불안정, 주문·체결 주의 패턴을 승인 사양의 code로 변환
  - kill switch·scheduler·dead letter 중복 문구는 `None` 반환
  - 매칭 실패와 파싱 실패는 `DigestTradeNotice("unknown")`
  - 원문 로깅 금지

- [ ] **Step 5: 중복·상한·전체 건수의 실패하는 테스트 작성**

  ```python
  def test_collect는_순서를지키고_중복제거후_5건과전체수를반환한다():
      warnings = (
          "no analysis result yet — will retry within entry window\n"
          "003960: entry dropped — liquidity: avg value 1 < 10",
          "003960: entry dropped — liquidity: avg value 1 < 10\n"
          "005930: entry dropped — already held (§6-3.3)\n"
          "000660: entry dropped — reentry cooldown (recently closed)\n"
          "035420: entry dropped — free slots exhausted (0)\n"
          "unexpected warning A\nunexpected warning B",
      )

      notices, count = collect_trade_notices(warnings)

      assert len(notices) == 5
      assert notices[0] == DigestTradeNotice("analysis_wait")
      assert notices[1] == DigestTradeNotice("liquidity", "003960", 1, 10)
      assert count == 7
  ```

  알 수 없는 원문 둘은 표시 notice 하나로 통합하지만 전체 고유 경고 수에는
  각각 포함돼 `외 N건` 계산에서 사라지지 않는다.

- [ ] **Step 6: collector 구현과 정규화 단위 테스트 통과**

  `collect_trade_notices()`는 각 warnings 문자열을 `splitlines()`하고,
  공백 줄을 제외한 원문 line 자체의 SHA-256 digest로 알 수 없는 항목 중복을
  내부에서만 판정한다. digest와 원문은 반환하지 않는다. 알려진 notice는
  dataclass equality로 중복 제거한다.

  Run:

  ```bash
  cd backend
  uv run pytest tests/notifications/test_digest_trade_notices.py -q
  ```

  Expected: PASS.

- [ ] **Step 7: 실제 read model 연결의 실패하는 통합 테스트 작성**

  `backend/tests/notifications/test_digest.py` SQLite fixture에 같은 거래일의
  mock run 두 개와 다른 환경 run 한 개를 추가한다. mock run warnings에는
  합성 분석 지연과 유동성 경고를 넣고 real run에는 다른 경고를 넣는다.

  ```python
  summary = DigestRunStore(engine, "mock", now=lambda: now).trading_summary(
      date(2026, 7, 29)
  )

  assert summary.notices == (
      DigestTradeNotice("analysis_wait"),
      DigestTradeNotice("liquidity", "003960", 252_557_535, 1_000_000_000),
  )
  assert summary.notice_count == 2
  ```

  다른 환경과 다른 거래일의 경고가 섞이지 않는 assertion을 함께 둔다.

- [ ] **Step 8: `DigestRunStore.trading_summary()`에 collector 연결**

  `_started_on()`이 반환한 run을 `started_at`, `id` 순으로 정렬해
  `collect_trade_notices(tuple(run.warnings for run in runs))`에 전달한다.
  반환된 `notices`, `notice_count`를 기존 `DigestSection` 생성의 keyword
  argument로 넘긴다. 기존 주문·손익·상태 query는 변경하지 않는다.

- [ ] **Step 9: 합성 E2E와 전체 관련 회귀 검증**

  Run:

  ```bash
  cd backend
  uv run pytest \
    tests/notifications/test_digest_trade_notices.py \
    tests/notifications/test_digest.py \
    tests/notifications/test_commands.py \
    tests/notifications/test_service.py \
    tests/notifications/test_notification_store.py \
    tests/notifications/test_morning_analysis_telegram_e2e.py -q
  ```

  Expected: PASS. DB 원문이 rendered body와 payload에 없고, 새 자동
  다이제스트에는 한국어 경고 절이 포함된다.

- [ ] **Step 10: 문서·상태·전체 비라이브 검증**

  회고에 Task 2의 정규화 패턴, unknown 격리, 중복·상한, 변경 위치와 실제
  검증 수치를 추가한다. `docs/STATUS.md` 최상단을 2026-07-29로 갱신하고
  다음 정상 16:10 자동 모의 다이제스트와 `/digest` 1회 읽기 전용 수용을
  다음 체크포인트로 기록한다.

  Run:

  ```bash
  cd backend
  uv run python -m compileall -q app tests
  uv run pytest -q -m "not live"
  cd ..
  git diff --check
  ```

  Expected: compileall과 diff check exit 0, 전체 비라이브 PASS, live 11건만
  deselected, 기존 Starlette warning 외 새 warning 없음.

- [ ] **Step 11: Task 2 리뷰 패널·재검증·커밋 승인**

  `$ohmystock-review-panel` 네 상시 리뷰어가 같은 Task 2 diff를 독립 검토한다.
  store adapter는 DB 경고만 읽고 broker·TR·주문·인증을 변경하지 않으므로
  `broker-api-expert`는 호출하지 않는다. Critical/Important는 TDD 수정,
  해당 관점 재검토, Step 9·10 재실행 후 종결한다.

  사용자에게 다음 예상 커밋과 실제 포함 파일을 먼저 제시하고 승인 뒤에만
  커밋한다.

  ```text
  feat(telegram): summarize digest trade warnings
  ```

  예상 포함 파일:

  ```text
  backend/app/store/digest_trade_notices.py
  backend/app/store/notification_store.py
  backend/tests/notifications/test_digest_trade_notices.py
  backend/tests/notifications/test_digest.py
  docs/retrospectives/2026-07-29-readable-digest-trade-warnings.md
  docs/STATUS.md
  ```
