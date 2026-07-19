# Phase 5 사전 게이트 정비 구현 계획서

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development to implement this plan
> task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**목표:** Phase 5(트레이딩 엔진) 착수 전 코드 게이트 4건 해소 — 분석
감사성(advice 저장·타임스탬프·기준일 노출), 프롬프트 비용/한계 명시,
쓰기 엔드포인트 보호.

**근거 문서:** `docs/retrospectives/2026-07-18-phase4-ai-analysis.md` §6,
`docs/STATUS.md` "Phase 5 착수 전" 블록. 사용자 결정(2026-07-18): economist
폴백은 **현행 유지**(중립+상한 5 — 트레이더 패널 권고 기각, 결정 로그 #23),
쓰기 보호는 **API 키 헤더 + CORS 제한** 채택.

**아키텍처:** 기존 계층 유지 — 스키마 변경은 store+Alembic, 프롬프트는
domain, 보호는 api 의존성 + main.py 조립. 신규 의존성 없음.

## Global Constraints

- 문서·주석·테스트명 한국어("왜" 중심 주석), CLAUDE.md 규칙 준수.
- 시크릿은 SecretStr, 값 로그/예외 노출 금지. 커밋 메시지 AI 흔적 금지.
- TDD: RED 캡처 → 구현 → GREEN 캡처(`../.superpowers/sdd/p5pre-task-N-{red,green}.txt`).
- 테스트 기준선: **299 passed, 10 deselected** — 회귀 0.
- 구현자는 패널을 스스로 돌리지 않는다(코디네이터가 디스패치).

---

### Task 1: 분석 감사성 보강 (advice 저장 + 타임스탬프 + 기준일)

**Files:**
- Modify: `backend/app/store/models.py` (AnalysisRunRow)
- Create: `backend/alembic/versions/0006_analysis_advice.py`
- Modify: `backend/app/store/analysis_store.py` (finish_run, latest_results)
- Modify: `backend/app/domain/analysis/service.py` (AnalysisProgress, _run, _fail, _set)
- Modify: `backend/app/api/analyze.py` (status 응답)
- Test: `backend/tests/store/test_analysis_store.py`, `backend/tests/analysis/test_service.py`, `backend/tests/test_api_analyze.py`

**Interfaces:**
- Produces: `AnalysisRunRow.max_picks_advice: int | None`(신규 칼럼),
  `finish_run(..., max_picks_advice: int | None = None)`,
  `latest_results()` dict에 `max_picks_advice`·`score_reference_date`(ISO)
  추가, `AnalysisProgress.started_at/finished_at: str | None`(ISO),
  `/analyze/status` 응답에 `started_at`/`finished_at`.

- [ ] **Step 1: 실패하는 테스트.**
  - store 왕복 테스트(`test_run_라이프사이클과_결과_왕복`)에 추가:
    `finish_run(..., max_picks_advice=3)` 후
    `latest["max_picks_advice"] == 3`,
    `latest["score_reference_date"] == "<픽스처 score run reference_date ISO>"`
    (기존 픽스처가 만드는 ScoreRunRow의 reference_date와 일치 단언 —
    구현자는 픽스처를 읽고 실제 값 사용).
  - 서비스 테스트: 성공 런 후 `progress().finished_at is not None`이고
    `started_at <= finished_at`(ISO 문자열 비교), 시계는 주입 가능해야 함
    (아래 Step 3 `now` 파라미터). 실패 런(`_fail`)도 `finished_at` 스탬프.
  - API 테스트: FakeAnalysis progress에 `started_at`/`finished_at` 필드
    추가, status 응답에 두 필드 포함 단언.
- [ ] **Step 2: RED 캡처** — `uv run pytest tests/store/test_analysis_store.py tests/analysis/test_service.py tests/test_api_analyze.py -q > ../.superpowers/sdd/p5pre-task-1-red.txt 2>&1`
- [ ] **Step 3: 구현.**
  - `models.py`: `max_picks_advice: Mapped[int | None] = mapped_column(Integer, nullable=True)`
    — nullable인 이유(과거 런 + economist 폴백 발동 여부와 무관하게 값
    자체는 항상 있지만 실패 런은 파이프라인 미도달로 None)를 주석으로.
  - `0006_analysis_advice.py`: `op.add_column("analysis_runs", sa.Column("max_picks_advice", sa.Integer(), nullable=True))` / downgrade는 drop_column.
  - `analysis_store.py`: `finish_run` 키워드 파라미터 추가 + 대입.
    `latest_results`: run 조회 시 `ScoreRunRow.reference_date`를 JOIN
    (`select(AnalysisRunRow, ScoreRunRow.reference_date).join(ScoreRunRow, AnalysisRunRow.score_run_id == ScoreRunRow.id)`)
    으로 함께 가져와 `"score_reference_date": reference_date.isoformat()`,
    `"max_picks_advice": run.max_picks_advice` 추가. 소비자(Phase 5/7)가
    "픽의 데이터 as-of 일자"를 별도 조회 없이 알 수 있게 하는 것이 목적
    (P4-T6 트레이더 패널) — docstring에 명시.
  - `service.py`: `AnalysisProgress`에 `started_at: str | None = None`,
    `finished_at: str | None = None`. `AnalysisService.__init__`에
    `now: Callable[[], datetime] | None = None` 주입(기본
    `lambda: datetime.now(timezone.utc)`), `_set`에 두 타임스탬프 파라미터
    추가(기존 호출부는 `_run` 진입 시 `started = self._now().isoformat()`
    한 번 고정해 전달, 종결 `_set`(succeeded/`_fail`)만
    `finished=self._now().isoformat()`). 성공 경로 `finish_run` 호출에
    `max_picks_advice=result.market.max_picks_advice` 추가.
  - `analyze.py`: status body에 `started_at`/`finished_at`(None 허용) 추가.
- [ ] **Step 4: GREEN + 전체 회귀** — `uv run pytest tests -q > ../.superpowers/sdd/p5pre-task-1-green.txt 2>&1` (기준선+신규, 0 fail)
- [ ] **Step 5: 커밋** — `feat(analysis): persist advice + expose timestamps and reference date`

### Task 2: 트레이더 프롬프트 비용 연동 + 백테스트 한계 명시

**Files:**
- Modify: `backend/app/domain/analysis/config.py` (AnalysisConfig)
- Modify: `backend/app/domain/analysis/prompts.py`
- Test: `backend/tests/analysis/test_prompts.py`(기존 파일 확인 후 동일 파일)

**Interfaces:**
- Consumes: 기존 트레이더 프롬프트 빌더/시스템 프롬프트(구현자는
  `prompts.py`의 기존 구조 확인 — 시스템 프롬프트가 상수라면 cfg를 받는
  함수로 전환하되 `prompt_hash()` 계산 경로가 cfg 기본값 기준으로 안정
  유지되는지 확인).
- Produces: `AnalysisConfig.round_trip_cost_pct: float = 0.25`(왕복 비용,
  %p 단위 — 스펙 §5-5 갱신 대상), 비용 문구가 이 값으로 렌더링된 트레이더
  프롬프트, 백테스트 한계 2건 문구.

- [ ] **Step 1: 실패하는 테스트.** 트레이더 시스템 프롬프트(또는 빌더
  출력)에 대해: ① `"0.25"` (기본 비용값) 포함 + 하드코딩 문구
  `"0.2~0.3"` 부재, ② `"겹치"` 또는 `"자기상관"` 포함(중복 보유 겹침
  표본 한계), ③ `"국면"`+`"조건"` 계열 문구 포함(regime 미조건화 한계),
  ④ `round_trip_cost_pct=0.5`로 만든 cfg에서는 `"0.5"` 렌더링.
- [ ] **Step 2: RED 캡처** — `p5pre-task-2-red.txt`
- [ ] **Step 3: 구현.** config에 필드 추가(주석: 프롬프트 판단 기준
  Phase 5 실비용 설정과 연동하기 위한 SSOT — 값 변경은 스펙 갱신과
  함께). prompts.py의 비용 문단을
  `f"...거래비용(왕복 수수료·거래세 약 {cfg.round_trip_cost_pct}%p) 미차감..."` 형태로 치환하고, 판단 원칙에 두 줄 추가:
  - "- 평균수익률·승률·발생횟수는 보유기간이 겹치는 표본(중복 보유 허용)
    기반이라 자기상관으로 통계 신뢰도가 과대평가됩니다 — 발생횟수가 커
    보여도 유효 독립 표본은 그보다 훨씬 적다고 간주하세요."
  - "- 전략 통계는 시장 국면(regime)으로 조건화되지 않은 전체 기간
    값입니다 — 현재 국면과 다른 환경에서 쌓인 성과일 수 있으니 국면
    악화 시 확신을 추가로 낮추세요."
  PROMPT_VERSION을 한 단계 올린다(기존 표기 규칙 확인 후 동일 형식).
- [ ] **Step 4: GREEN + 전체 회귀** — `p5pre-task-2-green.txt`
- [ ] **Step 5: 커밋** — `feat(analysis): cost-linked trader prompt + backtest caveats`

### Task 3: 쓰기 엔드포인트 API 키 + CORS 제한

**Files:**
- Modify: `backend/app/core/config.py` (Settings)
- Create: `backend/app/api/security.py`
- Modify: `backend/app/api/{collect,score,analyze}.py` (POST에 의존성)
- Modify: `backend/app/main.py` (CORS)
- Modify: `backend/.env.example`, `.env.example`(루트 — 존재 시)
- Test: `backend/tests/test_api_security.py`(신규), 기존 API 테스트 최소 수정

**Interfaces:**
- Produces: `Settings.api_write_token: SecretStr | None = None`,
  `Settings.cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"`
  (콤마 구분 문자열 — pydantic-settings 리스트 파싱 함정 회피),
  `require_write_token` FastAPI 의존성, 모든 쓰기 POST(`/collect`,
  `/score`, `/analyze`)에 적용.

- [ ] **Step 1: 실패하는 테스트.** 미니 앱 패턴으로:
  ① 토큰 설정 + 헤더 없음 → 401, ② 잘못된 헤더 → 401, ③ 올바른
  `X-API-Key` → 통과(202/409 등 기존 동작), ④ 토큰 미설정 → 통과하되
  구현이 경고 로그를 남김(caplog 단언), ⑤ 토큰 비교는 `secrets.compare_digest`
  (타이밍 안전) 사용 — 테스트는 동작만 검증. GET(status/latest)은 보호
  대상 아님 단언.
- [ ] **Step 2: RED 캡처** — `p5pre-task-3-red.txt`
- [ ] **Step 3: 구현.** `api/security.py`:

```python
"""쓰기 엔드포인트 보호 — X-API-Key 헤더 검증 (P3/P4 보안 패널 이월,
사용자 결정 2026-07-18: API 키 + CORS 제한).

토큰 미설정 시에는 차단하지 않고 기동 시 경고만 남긴다 — 모의투자
로컬 개발 편의. Phase 5 실전 전환 게이트에서 "토큰 설정 필수"로
승격한다(STATUS.md PRE-GATE #7과 함께 재평가)."""

import secrets

from fastapi import Header, HTTPException, Request


async def require_write_token(
        request: Request,
        x_api_key: str | None = Header(default=None)) -> None:
    token = request.app.state.settings.api_write_token
    if token is None:
        return  # 미설정 — main.py 기동 시 경고 로그가 이미 남음
    if x_api_key is None or not secrets.compare_digest(
            token.get_secret_value(), x_api_key):
        raise HTTPException(status_code=401, detail="invalid or missing API key")
```

  (전제: `app.state.settings`가 없으면 lifespan에서 저장하도록 main.py에
  한 줄 추가 — 기존 조립 확인 후 동일 스타일로.) 각 라우터 POST에
  `dependencies=[Depends(require_write_token)]`. main.py CORS를
  `allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()]`
  로 교체하고 `allow_headers`에 `X-API-Key` 포함 확인. 토큰 미설정 시
  lifespan에서 `logger.warning` 1회. `.env.example`에 두 키 문서화(값
  예시는 더미).
- [ ] **Step 4: GREEN + 전체 회귀** — `p5pre-task-3-green.txt`
- [ ] **Step 5: 커밋** — `feat(api): write-endpoint api key + cors origin allowlist`

---

## 계획 자체 점검

- **커버리지:** STATUS PRE-GATE #2(→T1)·#4(→T1)·#5(→T2)·#6(→T3) 해소.
  #3은 사용자 결정으로 종결(코드 무변경), #1(kt00018)·#7(외부 추론
  재평가)은 Phase 5 본편으로 이월 — 본 계획 범위 아님을 명시.
- **자리표시자:** 구현자가 기존 파일을 읽어야 하는 지점(프롬프트 빌더
  구조, store 픽스처 값)은 "확인 후" 지시로 명시 — 값 창작 금지.
- **타입 일관성:** finish_run 확장은 키워드 전용이라 기존 위치 인자
  호출부(service.py `_fail`)와 호환. AnalysisProgress 확장은 기본값
  None이라 기존 생성부 호환.
