# 읽기 쉬운 Telegram 장 마감 다이제스트 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기존 다이제스트의 저장·재시도·조회 계약을 유지하면서 16:10 Telegram 장 마감 메시지를 한국어 핵심 요약 형식으로 표시한다.

**Architecture:** `Digest`가 이미 보유한 version 1 payload를 입력으로 사용하는 순수 presenter를 `backend/app/domain/notifications/digest.py` 안에 둔다. `Digest.body`와 `render_retained_digest()`는 동일한 presenter 경로를 사용하고, DB read model·Telegram adapter·명령 처리·스케줄러는 변경하지 않는다.

**Tech Stack:** Python 3.12, dataclass, pytest, FastAPI 백엔드의 기존 Telegram plain-text formatter

## Global Constraints

- 승인된 설계는 `docs/specs/2026-07-28-readable-telegram-digest-design.md`를 기준으로 한다.
- `Digest.payload`의 version 1 구조, idempotency key, 16:10 생성 시각, 최근 7거래일 catch-up, sender retry와 24시간 보존 계약을 변경하지 않는다.
- presenter는 broker, SQL, Telegram adapter를 호출하지 않는 순수 로직이어야 한다.
- `mock`은 `모의투자`, `real`은 `🚨 실전`으로 표시하되 기술용 idempotency key는 사용자 본문에서 숨긴다.
- JSON, UTC ISO 시각, 내부 예외와 알 수 없는 `failed_fields` 원문을 사용자 본문에 노출하지 않는다.
- 계좌 조회 실패를 `0원`으로 바꾸거나 검증되지 않은 총자산을 계산하지 않는다.
- 기존에 전송되어 delivery part로 보존된 다이제스트는 재작성하지 않는다.
- 실제 Telegram, 키움 API, 주문, 분석 재실행과 운영 DB를 검증 과정에서 호출하지 않는다.

---

## 파일 구조

- `backend/app/domain/notifications/digest.py`
  - 기존 `Digest`, `DigestSection`, `DigestAccount` 계약을 유지한다.
  - 다이제스트 전용 한국어 상태·시장 국면·경고 매핑과 안전한 scalar 표시 helper를 추가한다.
  - `Digest.body`가 새 순수 presenter의 결과를 반환하게 한다.
- `backend/tests/notifications/test_digest.py`
  - 정상·부분 실패·손상 값·환경별 exact 본문과 비노출 계약을 검증한다.
  - version 1 retained payload가 `Digest.body`와 같은 presenter를 사용함을 검증한다.
- `backend/tests/notifications/test_commands.py`
  - `/digest`가 보존 payload만 사용하고 원래 delivery part가 있으면 그 part를 재전송하는 기존 계약을 회귀 검증한다.
- `docs/retrospectives/2026-07-28-readable-telegram-digest.md`
  - 요청, 기존 상태, 설계 판단, 변경 위치, 검증과 운영 수용 범위를 기록한다.
- `docs/STATUS.md`
  - 최신 완료 작업과 다음 16:10 읽기 전용 수용 체크포인트를 재개 지점에 반영한다.

---

### Task 1: 장 마감 다이제스트 순수 presenter와 호환 회귀

**Files:**
- Modify: `backend/app/domain/notifications/digest.py`
- Modify: `backend/tests/notifications/test_digest.py`
- Modify: `backend/tests/notifications/test_commands.py`
- Create: `docs/retrospectives/2026-07-28-readable-telegram-digest.md`
- Modify: `docs/STATUS.md`

**Interfaces:**
- Consumes: `Digest(trading_day, run_environment, pipeline, trading, account)`, `DigestSection.facts`, `DigestSection.failed_fields`, `DigestAccount`, 기존 `render_parts()`
- Produces: `Digest.body -> str`, `Digest.bodies -> tuple[str, ...]`, `render_retained_digest(payload: Mapping[str, object]) -> str`
- Preserves: `Digest.payload` version 1, `Digest.idempotency_key`, `render_retained_digest()`의 schema 검증과 `/digest`의 저장 delivery part 우선 재전송

- [ ] **Step 1: 정상 모의투자 본문의 실패하는 exact 테스트 작성**

  `backend/tests/notifications/test_digest.py`에 모든 정상 필드를 가진 fixture와 다음 기대 본문을 추가한다.

  ```python
  def test_digest본문은_정상상태를_한국어핵심요약으로_표시한다():
      digest = Digest(
          date(2026, 7, 28),
          "mock",
          DigestSection(
              {
                  "collection_status": "done",
                  "collection_reference_day": "2026-07-27",
                  "scoring_status": "succeeded",
                  "scoring_reference_day": "2026-07-27",
                  "candidate_count": 2519,
                  "analysis_status": "succeeded",
                  "analysis_score_reference_day": "2026-07-27",
                  "pick_count": 0,
                  "market_regime": "risk_off",
              },
              kst(2026, 7, 28, 8, 28),
          ),
          DigestSection(
              {
                  "order_count": 0,
                  "entry_order_count": 0,
                  "exit_order_count": 0,
                  "current_position_count": 0,
                  "realized_pnl": 0,
                  "realized_pnl_confidence": "estimated",
                  "kill_switch_run_count": 0,
                  "scheduler_gave_up_count": 0,
                  "scheduler_dead_count": 0,
                  "dead_letter_count": 0,
              },
              kst(2026, 7, 28, 16, 10),
          ),
          DigestAccount(
              9_979_053, 0, 0, 0, "estimated", "cached", (),
              kst(2026, 7, 28, 16, 10), date(2026, 7, 28),
          ),
      )

      assert digest.body == (
          "📋 장 마감 다이제스트 · 모의투자\n"
          "2026년 7월 28일\n\n"
          "📊 오늘의 분석\n"
          "데이터 수집      완료 · 기준 7월 27일\n"
          "종목 점수 계산   완료 · 2,519종목\n"
          "AI 분석          완료 · 위험회피\n"
          "최종 진입 후보   없음\n\n"
          "💼 자동매매\n"
          "매수 주문        0건\n"
          "매도 주문        0건\n"
          "관리 포지션      0개\n"
          "실현손익         0원 (추정)\n\n"
          "💰 계좌\n"
          "예수금           9,979,053원\n"
          "보유주식 평가    0원\n"
          "평가손익         0원\n"
          "실현손익         0원 (추정)\n\n"
          "🕖 다음 일정\n"
          "오늘 19:00 데이터 수집"
      )
  ```

- [ ] **Step 2: 정상 본문 테스트가 구형 JSON 형식 때문에 실패하는지 확인**

  Run:

  ```bash
  cd backend
  uv run pytest tests/notifications/test_digest.py::test_digest본문은_정상상태를_한국어핵심요약으로_표시한다 -q
  ```

  Expected: FAIL. 실제 값에는 `다이제스트 ID`, `파이프라인(` 또는 JSON이 포함되고 기대한 한국어 섹션 형식과 다르다.

- [ ] **Step 3: 경계값·환경·경고의 실패하는 매개변수 테스트 작성**

  `backend/tests/notifications/test_digest.py`에 다음 사례를 각각 독립 테스트로 추가한다.

  ```python
  @pytest.mark.parametrize(
      ("stored", "displayed"),
      [
          ("risk_on", "위험선호"),
          ("neutral", "중립"),
          ("risk_off", "위험회피"),
          ("unexpected", "확인 불가"),
          (None, "확인 불가"),
      ],
  )
  def test_digest본문은_시장국면을_허용목록으로_표시한다(stored, displayed):
      digest = _readable_digest(market_regime=stored)
      assert f"AI 분석          완료 · {displayed}" in digest.body


  def test_digest본문은_실전환경과_후보_양수와_exact손익을_표시한다():
      digest = _readable_digest(
          run_environment="real",
          pick_count=3,
          realized_pnl=-12_345,
          realized_pnl_confidence="exact",
      )

      assert digest.body.startswith("📋 장 마감 다이제스트 · 🚨 실전")
      assert "최종 진입 후보   3종목" in digest.body
      assert "실현손익         -12,345원" in digest.body
      assert "-12,345원 (추정)" not in digest.body


  def test_digest본문은_누락과_손상값을_추측없이_경고한다():
      digest = _readable_digest(
          pipeline_failed=("collection", "scoring", "unknown_internal"),
          trading_failed=("trade_runs", "unknown_internal"),
          account_source="unavailable",
          account_failed=("account_snapshot", "unknown_internal"),
          candidate_count=-1,
          entry_order_count=-1,
      )

      assert "계좌 스냅샷을 조회하지 못했습니다." in digest.body
      assert digest.body.count("- 일부 상태 확인 불가") == 1
      assert digest.body.count("- 데이터 수집 결과 없음") == 1
      assert digest.body.count("- 종목 점수 계산 결과 없음") == 1
      assert digest.body.count("- 거래 실행 기록 없음") == 1
      assert digest.body.count("- 계좌 스냅샷 조회 실패") == 1
      assert "unknown_internal" not in digest.body
      assert "매수 주문        확인 불가" in digest.body
  ```

  `_readable_digest()` fixture는 Step 1의 정상 DTO를 기본값으로 만들고 keyword
  인자로 해당 field만 교체한다. `candidate_count=-1`은 점수 계산 대상 수를
  `확인 불가`로 표시하며, 음수 주문·포지션·후보 수는 사용자에게 그대로
  노출하지 않는다.

- [ ] **Step 4: 비노출·계좌 부분 실패·retained 동일성의 실패하는 테스트 작성**

  다음 계약을 추가한다.

  ```python
  def test_digest본문은_기술식별자_JSON_UTC시각을_노출하지않는다():
      body = _readable_digest().body

      assert "digest:mock:" not in body
      assert "다이제스트 ID" not in body
      assert '{"' not in body
      assert "+00:00" not in body
      assert "T08:" not in body


  def test_digest본문은_계좌부분실패에서_가용금액만_보존한다():
      digest = _readable_digest(
          account_total_eval=None,
          account_failed=("total_eval",),
      )

      assert "예수금           9,979,053원" in digest.body
      assert "보유주식 평가    확인 불가" in digest.body
      assert "- 일부 계좌 정보 확인 불가" in digest.body
      assert "total_eval" not in digest.body


  def test_retained_v1은_새_digest본문과_동일한_presenter를_사용한다():
      digest = _readable_digest()

      assert render_retained_digest(deepcopy(digest.payload)) == digest.body
  ```

  기존 `test_digest본문은_각_read_model의_누락필드를_명시한다`는 내부 필드명
  노출을 기대하므로, 한국어 경고와 알 수 없는 필드 비노출을 기대하도록
  교체한다. 기존 schema 손상 거부 테스트와 `Digest.body == retained renderer`
  테스트는 유지한다.

- [ ] **Step 5: 새 표시 회귀들이 모두 실패하는지 확인**

  Run:

  ```bash
  cd backend
  uv run pytest tests/notifications/test_digest.py -q
  ```

  Expected: 새 표시 테스트는 FAIL하고 planner, payload schema, read model,
  materialization 테스트는 기존처럼 PASS한다.

- [ ] **Step 6: 최소 순수 presenter 구현**

  `backend/app/domain/notifications/digest.py`에서 구형 `_compact_json()`,
  `_as_of()`, `_failed()` 기반 본문 조립을 다음 책임의 private helper들로
  교체한다.

  ```python
  _ENVIRONMENT_LABELS = {"mock": "모의투자", "real": "🚨 실전"}
  _REGIME_LABELS = {
      "risk_on": "위험선호",
      "neutral": "중립",
      "risk_off": "위험회피",
  }
  _FAILED_FIELD_WARNINGS = {
      "collection": "데이터 수집 결과 없음",
      "scoring": "종목 점수 계산 결과 없음",
      "analysis": "AI 분석 결과 없음",
      "trade_runs": "거래 실행 기록 없음",
      "account_snapshot": "계좌 스냅샷 조회 실패",
  }


  def _render_digest(digest: Digest) -> str:
      sections = [
          _render_digest_header(digest),
          _render_pipeline(digest.pipeline),
          _render_trading(digest.trading),
          _render_account(digest.account),
      ]
      warnings = _digest_warnings(digest)
      if warnings:
          sections.append("⚠️ 확인 필요\n" + "\n".join(
              f"- {warning}" for warning in warnings
          ))
      sections.append("🕖 다음 일정\n오늘 19:00 데이터 수집")
      return "\n\n".join(sections)
  ```

  세부 helper 계약은 다음과 같이 고정한다.

  - `_render_digest_header()`는 환경 라벨과 `YYYY년 M월 D일`만 반환한다.
  - `_render_pipeline()`은 `done`/`succeeded`만 `완료`로 인정한다.
  - 기준일은 `date.fromisoformat()`이 성공할 때만 `M월 D일`로 표시한다.
  - `candidate_count`, `pick_count`, 주문 수, 포지션 수는 `type(value) is int`
    이고 0 이상일 때만 숫자로 표시한다. `bool`은 숫자로 인정하지 않는다.
  - 금액은 `type(value) is int`일 때 `f"{value:,}원"`으로 표시한다.
  - 손익 신뢰도 `estimated`만 ` (추정)`을 붙이고 `exact`는 붙이지 않는다.
    그 밖의 신뢰도는 금액을 보존하되 경고 `일부 상태 확인 불가`에 포함한다.
  - 계좌 `source == "unavailable"`은 한 문장만 표시하고 금액을 만들지 않는다.
  - 계좌가 사용 가능하면 네 금액을 별도 줄로 표시하고 `None`은 `확인 불가`로
    표시한다.
  - 모든 `failed_fields`를 단일 순서로 순회해 알려진 값은 고정 한국어 경고로,
    알 수 없는 값은 `일부 상태 확인 불가`로 변환하며 중복을 제거한다.
  - 계좌의 알려지지 않은 부분 필드 실패는 `일부 계좌 정보 확인 불가`로
    표시하되 그 원문은 노출하지 않는다.
  - `Digest.body`는 `return _render_digest(self)`만 수행한다.
  - payload 생성, retained parsing, `Digest.bodies`와 `render_parts()`는
    변경하지 않는다.

- [ ] **Step 7: 다이제스트 단위 테스트 통과 확인**

  Run:

  ```bash
  cd backend
  uv run pytest tests/notifications/test_digest.py -q
  ```

  Expected: PASS. 계좌 전체 실패 테스트는 `0원`을 포함하지 않고, payload
  schema 손상은 계속 `ValueError`로 거부된다.

- [ ] **Step 8: `/digest`가 저장 delivery part를 그대로 재전송하는 회귀 강화**

  `backend/tests/notifications/test_commands.py`의 digest report fake가
  `(payload, delivery_bodies)`를 따로 받을 수 있게 하고 다음 테스트를 추가한다.

  ```python
  class DigestReports:
      def __init__(self, payload=None, delivery_bodies=None):
          self.payload = payload
          self.delivery_bodies = delivery_bodies
          self.calls = 0

      def latest_digest(self):
          self.calls += 1
          if self.payload is None:
              return None
          return (
              self.payload,
              self.delivery_bodies or (render_retained_digest(self.payload),),
          )


  @pytest.mark.anyio
  async def test_digest명령은_기존_delivery_part를_새형식으로_재작성하지않는다(
          tmp_path):
      engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'digest-parts.db'}")
      Base.metadata.create_all(engine)
      inbox = TelegramInboxStore(engine, now=lambda: NOW)
      commands = TelegramCommandStore(engine, now=lambda: NOW)
      control = Control([], commands)
      reports = DigestReports(
          _digest_payload(),
          delivery_bodies=("[digest-mock-2026-07-24] [1/1]\n구형 원문",),
      )
      worker = CommandProcessor(
          inbox, commands, control, "worker", chat_hash=CHAT, now=lambda: NOW,
          analysis_reports=AnalysisReports(), digest_reports=reports,
      )
      inbox.persist_batch_and_offset(
          [{
              "update_id": 38,
              "operator_hash": OP,
              "command": "digest",
              "received_at": NOW,
          }],
          39,
      )

      result = await worker.process_next()

      assert result.response_parts == (
          "[digest-mock-2026-07-24] [1/1]\n구형 원문",
      )
      assert control.calls == []
      assert reports.calls == 1
  ```
  
  production `commands.py`는 이 회귀가 실패하지 않는 한 수정하지 않는다.

- [ ] **Step 9: Telegram command·service 관련 회귀 통과 확인**

  Run:

  ```bash
  cd backend
  uv run pytest \
    tests/notifications/test_digest.py \
    tests/notifications/test_commands.py \
    tests/notifications/test_service.py \
    tests/notifications/test_notification_store.py \
    tests/notifications/test_morning_analysis_telegram_e2e.py -q
  ```

  Expected: PASS. `/digest`는 broker/control을 호출하지 않고, 새 자동
  다이제스트는 새 본문을 materialize하며, 기존 delivery part는 원문 그대로
  반환한다.

- [ ] **Step 10: 구현 회고와 재개 지점 작성**

  `docs/retrospectives/2026-07-28-readable-telegram-digest.md`에 다음 절을
  실제 결과로 작성한다.

  ```markdown
  # 읽기 쉬운 Telegram 장 마감 다이제스트 회고

  ## 요청과 기존 상태
  ## 설계 판단
  ## 변경 파일과 정확한 위치
  ## 테스트 우선 구현 결과
  ## 검증 결과
  ## 변경하지 않은 안전 경계
  ## 배포 후 읽기 전용 수용
  ```

  `docs/STATUS.md` 최상단 재개 지점을 2026-07-28로 갱신하고 다음을 기록한다.

  - readable digest 구현 커밋과 검증 결과
  - 알고리즘·주문·DB schema·payload v1·스케줄러를 변경하지 않았음
  - 다음 정상 16:10 자동 모의 다이제스트와 그 뒤 `/digest` 1회만 관찰
  - 기존 보존 다이제스트는 구형 원문일 수 있음

- [ ] **Step 11: 포맷·정적·전체 비라이브 회귀 검증**

  Run:

  ```bash
  cd backend
  uv run python -m compileall -q app tests
  uv run pytest -q -m "not live"
  cd ..
  git diff --check
  rg -n "digest:mock:|다이제스트 ID|\\{\\\"" backend/app/domain/notifications/digest.py
  ```

  Expected:

  - `compileall` exit 0
  - 전체 비라이브 pytest PASS, live 테스트만 deselected
  - `git diff --check` 출력 없음
  - 마지막 `rg`는 idempotency key 생성·correlation ID 같은 내부 계약만
    찾을 수 있고 새 사용자 본문 문자열에서는 기술 ID나 JSON을 찾지 않는다.

- [ ] **Step 12: OhMyStock 독립 리뷰 패널 실행 및 발견 수정**

  `$ohmystock-review-panel`을 사용해 동일한 전체 diff를 다음 네 관점에서
  독립 읽기 전용 검토한다.

  - `senior-developer`: presenter 책임, 중복, 타입 경계, 테스트 품질
  - `senior-trader`: 시장 국면·후보·주문·포지션·손익 의미가 오해를 만들지
    않는지
  - `architecture-expert`: domain 순수성, payload·delivery 보존 계약,
    계층 경계
  - `security-expert`: 내부 필드·예외·JSON·민감 계좌 정보의 의도치 않은
    노출과 fail-closed 동작

  브로커 adapter, TR, 주문, 인증, pagination, PRE-GATE를 변경하지 않으므로
  `broker-api-expert`는 호출하지 않는다. Critical 또는 Important 발견은
  TDD로 수정하고 해당 관점 재검토와 Step 9·11 검증을 다시 통과한다.

- [ ] **Step 13: 커밋 전 사용자 승인 요청**

  검증·리뷰가 끝나면 실제 diff를 기준으로 전체 커밋 메시지와 포함 파일을
  사용자에게 먼저 제시한다. 예상 메시지는 다음과 같지만 실제 변경 범위가
  다르면 승인 전에 정확히 고친다.

  ```text
  feat(telegram): present readable trading digest
  ```

  예상 포함 파일:

  ```text
  backend/app/domain/notifications/digest.py
  backend/tests/notifications/test_digest.py
  backend/tests/notifications/test_commands.py
  docs/retrospectives/2026-07-28-readable-telegram-digest.md
  docs/STATUS.md
  ```

  명시적 승인 전에는 `git commit`을 실행하지 않는다. 승인 후 커밋에는
  AI 저자 표시나 `Co-Authored-By`를 넣지 않는다.
