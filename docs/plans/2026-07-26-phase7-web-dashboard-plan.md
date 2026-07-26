# Phase 7 반응형 웹 대시보드 구현 계획

> **에이전트 작업자 필수 하위 스킬:** 이 계획을 태스크별로 구현할 때
> `superpowers:subagent-driven-development`(권장) 또는
> `superpowers:executing-plans`를 사용한다. 진행 추적은 각 단계의
> 체크박스(`- [ ]`)를 갱신한다.

**목표:** OhMyStock 관리 거래의 기간별 성과와 포지션을 PC·태블릿·모바일에서
안전하게 조회하는 PrimeReact 웹 대시보드를 구축한다.

**아키텍처:** React SPA와 FastAPI는 독립 컨테이너로 배포한다. 프런트엔드
컨테이너가 정적 파일과 `/api` 역방향 프록시를 제공하고, 외부에서는
Tailscale Serve HTTPS 주소 하나만 노출한다. 대시보드 API는 PostgreSQL
읽기 모델만 사용하며 BrokerPort, 키움 API, 주문 경로를 호출하지 않는다.

**기술 스택:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16,
React 19, TypeScript 5, Vite 7, PrimeReact, Vitest, Testing Library,
Docker Compose v2, Tailscale Serve

## 전역 제약

- 초기 버전은 조회 전용이다. 거래 제어 UI와 쓰기 API 프록시를 만들지 않는다.
- 성과는 현재 `run_environment`의 OhMyStock 관리 포지션만 집계한다.
- 기본 기간은 Asia/Seoul 기준 최근 30일이고 사용자 지정 기간을 지원한다.
- 최초 진입, 기간 변경, 버튼, 모바일 당겨서 새로고침에만 조회한다.
- 자동 폴링과 WebSocket을 사용하지 않는다.
- 조회 과정에서 BrokerPort나 키움 API를 호출하지 않는다.
- 최신 가격이 없거나 stale이면 `0`이나 `peak_price`로 대체하지 않는다.
- 프런트와 backend 호스트 포트는 `127.0.0.1`에만 바인딩한다.
- 브라우저에는 거래 API 키, 키움 키, Telegram 토큰, 계좌 식별자를 저장하지
  않는다.
- 라이트·다크 시스템 연동, 수동 전환, 비민감 선택값 저장을 지원한다.
- 각 구현 태스크는 한국어 회고 작성 후 `$ohmystock-review-panel`의
  `senior-developer`, `senior-trader`, `architecture-expert`,
  `security-expert` 독립 리뷰를 통과해야 한다.
- 브로커 어댑터, TR, 주문·인증·페이지네이션, PRE-GATE를 변경하면
  `broker-api-expert`를 추가한다.
- Critical·Important 발견은 수정하고 해당 관점 재검토를 통과한다.
- 커밋 전 전체 커밋 메시지와 포함 파일을 사용자에게 제시하고 명시적 승인을
  기다린다. `Co-Authored-By` 등 AI 저자 표시는 넣지 않는다.

---

## 파일 책임 지도

### 백엔드

- `backend/app/domain/dashboard/models.py`: 프레임워크 독립 성과 입력·출력
  값 객체와 집계 규칙
- `backend/app/store/dashboard_store.py`: 기간·환경별 SQL 읽기와 손상 행 격리
- `backend/app/api/dashboard.py`: 쿼리 검증과 JSON 응답 변환
- `backend/app/store/models.py`: 최신 mark 가격과 시각 영속 컬럼
- `backend/app/store/trading_store.py`: 거래 감시가 받은 mark의 원자 갱신
- `backend/app/domain/trading/monitor.py`: 기존 시세 관측 시 mark 저장 호출
- `backend/app/main.py`: dashboard router와 store 조립

### 프런트엔드

- `frontend/src/api/dashboard.ts`: DTO, 런타임 응답 검증, fetch
- `frontend/src/features/dashboard/useDashboard.ts`: 조회 상태와 중복 요청 제어
- `frontend/src/features/dashboard/DashboardPage.tsx`: 화면 조립
- `frontend/src/features/dashboard/components/*`: 기간, KPI, 차트, 포지션,
  거래, 오류·빈 상태의 작은 표현 단위
- `frontend/src/features/dashboard/format.ts`: KST·원화·퍼센트 포맷
- `frontend/src/hooks/usePullToRefresh.ts`: 최상단 당김 제스처
- `frontend/src/theme/*`: PrimeReact preset과 테마 상태
- `frontend/nginx.conf`: SPA 정적 제공과 읽기 API allowlist 프록시
- `frontend/Dockerfile`: production 정적 빌드와 웹 서버 이미지

### 문서

- `docs/architecture/system-overview.md`: Electron 제거와 웹 컨테이너 경계
- `AGENTS.md`: 지속 런타임 불변조건 변경
- `docs/STATUS.md`: Phase 7 진행·수용 상태
- `docs/retrospectives/2026-07-26-phase7-*.md`: 태스크별 회고

---

### Task 1: 관리 포지션 최신 mark 가격 영속

**파일**

- 수정: `backend/app/store/models.py`
- 수정: `backend/app/store/trading_store.py`
- 수정: `backend/app/domain/trading/models.py`
- 수정: `backend/app/domain/trading/monitor.py`
- 생성: `backend/migrations/versions/0015_trade_position_mark.py`
- 수정: `backend/tests/store/test_models_migration.py`
- 수정: `backend/tests/store/test_trading_store.py`
- 수정: `backend/tests/trading/test_monitor.py`
- 생성: `docs/retrospectives/2026-07-26-phase7-position-mark.md`

**인터페이스**

- 생성: `TradePosition.mark_price: int | None`
- 생성: `TradePosition.marked_at: datetime | None`
- 생성: `TradingStore.update_position_mark(position_id: int, price: int,
  marked_at: datetime) -> None`
- 후속 소비자: Task 2의 `DashboardStore`

- [ ] **Step 1: 마이그레이션·모델 실패 테스트 작성**

`test_models_migration.py`에 0014→0015 upgrade와 downgrade에서
`trade_positions.mark_price BIGINT NULL`,
`trade_positions.marked_at TIMESTAMPTZ NULL`의 존재·제거를 검증한다.
`test_trading_store.py`에는 mark 저장 후 같은 환경의 열린 포지션에서 가격과
UTC-aware 시각이 복원되는 테스트를 추가한다.

- [ ] **Step 2: 실패 확인**

실행:

```bash
cd backend
uv run pytest tests/store/test_models_migration.py tests/store/test_trading_store.py -q
```

예상: 0015 revision과 `update_position_mark`가 없어 실패한다.

- [ ] **Step 3: nullable mark 스키마와 저장 계약 구현**

0015는 기존 행을 추정 backfill하지 않는다. `TradePositionRow`와
`TradePosition`에 nullable 필드를 추가하고 `_row_to_position` 및 저장
경로를 맞춘다. `update_position_mark`는 다음을 검증한다.

```python
if price <= 0:
    raise ValueError("mark price must be positive")
if marked_at.tzinfo is None:
    raise ValueError("marked_at must be timezone-aware")
```

해당 `position_id`가 없으면 `ValueError`를 내고, 한 트랜잭션에서 두 컬럼을
함께 갱신한다.

- [ ] **Step 4: 거래 감시 연결 테스트를 먼저 작성**

`test_monitor.py`에 감시가 이미 얻은 현재가로 방어선 평가를 수행할 때 동일한
가격·시각이 mark 저장 포트로 한 번 전달되는지 검증한다. 가격 조회 실패
사이클에는 이전 mark를 지우거나 새 시각으로 꾸미지 않는 테스트도 추가한다.

- [ ] **Step 5: 감시 경로 최소 연결**

새 브로커 호출 없이 기존 monitor cycle의 유효한 현재가와 주입 시계를
`update_position_mark`로 전달한다. mark 저장 실패는 해당 사이클 경고와
검색 가능한 로그를 남기되 주문 판단값을 바꾸지 않는다.

- [ ] **Step 6: 관련 회귀 실행**

```bash
cd backend
uv run pytest tests/store/test_models_migration.py tests/store/test_trading_store.py tests/trading/test_monitor.py -q
```

예상: 전부 PASS. live 테스트는 실행하지 않는다.

- [ ] **Step 7: 회고와 리뷰 패널**

회고에 `peak_price`를 현재가로 재사용하지 않은 이유, nullable migration,
거래 감시 실패 격리, 검증 결과를 기록한다. `$ohmystock-review-panel` 네
관점으로 같은 diff를 리뷰하고 Critical·Important를 해소한다.

- [ ] **Step 8: 커밋 승인**

제안 메시지:

```text
feat(trading): persist managed position mark prices
```

전체 포함 파일을 제시하고 사용자 승인 뒤에만 커밋한다.

---

### Task 2: 대시보드 성과 도메인과 SQL 읽기 모델

**파일**

- 생성: `backend/app/domain/dashboard/__init__.py`
- 생성: `backend/app/domain/dashboard/models.py`
- 생성: `backend/app/store/dashboard_store.py`
- 생성: `backend/tests/dashboard/test_models.py`
- 생성: `backend/tests/store/test_dashboard_store.py`
- 생성: `docs/retrospectives/2026-07-26-phase7-dashboard-read-model.md`

**인터페이스**

- 생성: `DashboardPeriod(start: date, end: date, timezone: str)`
- 생성: `DashboardOverview`
- 생성: `DashboardStore.overview(period: DashboardPeriod,
  run_environment: str, now: datetime) -> DashboardOverview`
- Task 3이 `DashboardOverview`를 HTTP DTO로 직렬화한다.

- [ ] **Step 1: 순수 집계 규칙 실패 테스트 작성**

다음 사례를 고정 fixture로 작성한다.

- 기간 전에 진입하고 기간 안에 청산한 포지션 포함
- 기간 안에 진입하고 기간 뒤에 청산한 포지션 제외
- 승·패·보합 거래 수와 승률
- 확정 수익률 = `sum(realized_pnl) / sum(entry_price * quantity) * 100`
- 같은 포지션의 분할 주문·체결이 거래 1건
- mark가 없거나 stale인 열린 포지션의 평가손익 `None`
- 일부 열린 포지션만 mark가 유효하면 총손익에 `partial` 상태
- 손상 행 제외 수 보존

- [ ] **Step 2: 실패 확인**

```bash
cd backend
uv run pytest tests/dashboard/test_models.py -q
```

예상: `app.domain.dashboard`가 없어 collection 단계에서 실패한다.

- [ ] **Step 3: 프레임워크 독립 모델 구현**

`Decimal`로 비율을 계산하고 0원 투자금이면 수익률을 `None`으로 둔다.
집계 출력은 최소한 다음 상태를 명시한다.

```python
Literal["complete", "partial", "unavailable"]
Literal["recorded", "estimated", "unavailable"]
```

API 문자열이나 SQLAlchemy Row를 domain에 넣지 않는다.

- [ ] **Step 4: SQL 읽기 실패 테스트 작성**

SQLite 테스트 DB에 mock·real·replay 환경을 섞고 다음을 검증한다.

- 요청 환경만 포함
- `closed_at`의 coarse UTC bound 뒤 KST 날짜 재검증
- 열린 상태만 포지션 영역에 포함
- 최근 거래는 `closed_at DESC, id DESC`
- 누적 곡선은 `closed_at ASC, id ASC`
- enum·가격·수량 손상 행은 정상 행과 격리
- dashboard 조회 중 broker fixture 호출 0회

- [ ] **Step 5: DashboardStore 구현**

`TradePositionRow`와 `TradeRunRow`를 명시적으로 join한다. 원시
`resp_body`와 계좌 식별자는 select하지 않는다. mark stale 기준은
`now - marked_at > timedelta(minutes=10)`으로 고정하고 응답에 기준을
포함한다.

비용 완전 대사 컬럼은 현재 없으므로 초기 `cost_basis`는
`estimated`다. `realized_pnl`이 없는 청산 행은 0으로 합산하지 않고
손상·불완전 건수로 표면화한다.

- [ ] **Step 6: 관련 회귀 실행**

```bash
cd backend
uv run pytest tests/dashboard/test_models.py tests/store/test_dashboard_store.py tests/store/test_trading_store.py -q
```

예상: 전부 PASS.

- [ ] **Step 7: 회고와 리뷰 패널**

환경 격리, KST 귀속, 비용 confidence, stale 기준, 손상 행 정책과 테스트
결과를 기록한다. 네 관점 리뷰와 필요한 재검토를 마친다.

- [ ] **Step 8: 커밋 승인**

제안 메시지:

```text
feat(dashboard): add managed performance read model
```

전체 포함 파일을 제시하고 사용자 승인 뒤에만 커밋한다.

---

### Task 3: 조회 전용 dashboard API

**파일**

- 생성: `backend/app/api/dashboard.py`
- 수정: `backend/app/main.py`
- 생성: `backend/tests/dashboard/test_api.py`
- 생성: `docs/retrospectives/2026-07-26-phase7-dashboard-api.md`

**인터페이스**

- 생성: `GET /dashboard/overview?from=YYYY-MM-DD&to=YYYY-MM-DD&timezone=Asia/Seoul`
- 소비: `DashboardStore.overview(...)`
- Task 4의 `fetchDashboardOverview`가 JSON 계약을 소비한다.

- [ ] **Step 1: API 계약 실패 테스트 작성**

다음을 TestClient로 검증한다.

- `from`, `to` 생략 시 KST 오늘을 끝으로 최근 30일
- 명시한 양 끝 날짜 포함
- `from > to`, 366일 초과, 잘못된 날짜는 422
- timezone은 `Asia/Seoul`만 허용
- 현재 `run_environment`가 store에 전달됨
- 응답에 `period`, `summary`, `equity_curve`, `positions`,
  `recent_trades`, `freshness`, `warnings` 존재
- store 예외는 내부 문자열 없이 503과 안정된 오류 코드 반환

- [ ] **Step 2: 실패 확인**

```bash
cd backend
uv run pytest tests/dashboard/test_api.py -q
```

예상: dashboard router가 없어 404.

- [ ] **Step 3: router와 앱 조립 구현**

`dashboard.py`는 Pydantic 응답 모델로 Decimal을 문자열이 아닌 JSON number로
일관되게 직렬화하고 모든 datetime은 timezone offset을 포함한다.
`main.py`는 기존 engine/session factory로 `DashboardStore`를 조립하고
router를 포함한다. 인증·broker·trading service 의존성을 추가하지 않는다.

- [ ] **Step 4: 보안·회귀 테스트 보강**

응답 직렬화 결과에 `resp_body`, `token`, `account_no`, `app_key`,
내부 exception 문구가 없음을 검사한다. dashboard GET이 operations control,
trading start/stop, broker mock을 호출하지 않는 spy 테스트를 추가한다.

- [ ] **Step 5: 관련 회귀 실행**

```bash
cd backend
uv run pytest tests/dashboard tests/test_api_security.py tests/test_app_lifespan.py -q
```

예상: 전부 PASS.

- [ ] **Step 6: 회고와 리뷰 패널**

HTTP 계약, 조회 상한, 오류 비노출, 쓰기 경로 비의존과 검증 결과를 기록한다.
네 관점 리뷰와 필요한 재검토를 마친다.

- [ ] **Step 7: 커밋 승인**

제안 메시지:

```text
feat(api): expose read-only dashboard overview
```

전체 포함 파일을 제시하고 사용자 승인 뒤에만 커밋한다.

---

### Task 4: Electron 제거와 React 웹 기반·데이터 계층

**파일**

- 삭제: `frontend/src/main/index.ts`
- 삭제: `frontend/src/preload/index.ts`
- 삭제: `frontend/src/preload/index.d.ts`
- 삭제: `frontend/electron-builder.yml`
- 삭제: `frontend/electron.vite.config.ts`
- 수정: `frontend/package.json`
- 수정: `frontend/pnpm-lock.yaml`
- 수정: `frontend/tsconfig.json`
- 수정: `frontend/tsconfig.web.json`
- 수정: `frontend/vitest.config.ts`
- 수정: `frontend/eslint.config.mjs`
- 생성: `frontend/vite.config.ts`
- 이동: `frontend/src/renderer/index.html` → `frontend/index.html`
- 이동: `frontend/src/renderer/src` → `frontend/src`
- 생성: `frontend/src/api/dashboard.ts`
- 생성: `frontend/src/features/dashboard/useDashboard.ts`
- 생성: `frontend/src/features/dashboard/__tests__/useDashboard.test.tsx`
- 생성: `docs/retrospectives/2026-07-26-phase7-react-foundation.md`

**인터페이스**

- 생성: `fetchDashboardOverview(period: DateRange, signal: AbortSignal):
  Promise<DashboardOverview>`
- 생성: `useDashboard(initialPeriod?: DateRange): DashboardQueryState`
- 생성: `DashboardQueryState.refresh(): Promise<void>`
- Task 5의 화면 컴포넌트가 이 hook만 소비한다.

- [ ] **Step 1: 순수 Vite 전환**

Electron 패키지와 scripts를 제거하고 다음 명령 계약을 만든다.

```json
{
  "scripts": {
    "dev": "vite",
    "build": "tsc --noEmit && vite build",
    "lint": "eslint .",
    "test": "vitest run",
    "typecheck": "tsc --noEmit"
  }
}
```

PrimeReact, PrimeIcons와 차트 의존성을 pnpm으로 추가해 lockfile에 고정한다.
차트는 `chart.js`와 `react-chartjs-2`를 사용하고 canvas와 동일한 데이터의
텍스트 요약을 제공한다.

- [ ] **Step 2: 웹 기반 검증**

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm typecheck
pnpm test
```

예상: Electron 전용 import가 하나라도 남아 있으면 실패한다. `rg -n
"electron|preload|127\\.0\\.0\\.1:8000|/ws" src package.json` 결과는 0건이어야
한다.

- [ ] **Step 3: API parser 실패 테스트 작성**

정상 fixture 파싱, 필수 필드 누락, `NaN`·무한대, 잘못된 날짜, 알 수 없는
상태 enum을 검증한다. fetch URL이 상대 경로
`/api/dashboard/overview?...`인지 검사한다.

- [ ] **Step 4: query hook 실패 테스트 작성**

다음을 fake fetch로 검증한다.

- mount 시 1회 조회
- 기간 변경 시 이전 요청 abort 후 새 조회
- 동시 refresh는 같은 진행 요청으로 병합
- refresh 실패 시 마지막 성공 data 유지
- `lastUpdatedAt`은 성공 때만 변경
- unmount 시 AbortController 취소
- interval·WebSocket 생성 없음

- [ ] **Step 5: 최소 데이터 계층 구현**

외부 상태 라이브러리를 추가하지 않고 `fetch`와 React hook으로 구현한다.
응답 parser는 화면 컴포넌트 밖에서 경계를 검증한다. 오류는 사용자용
안정된 code와 로깅용 cause를 분리하되 응답 body를 console에 출력하지 않는다.

- [ ] **Step 6: 프런트 회귀**

```bash
cd frontend
pnpm lint
pnpm test
pnpm typecheck
pnpm build
```

예상: 전부 PASS.

- [ ] **Step 7: 회고와 리뷰 패널**

Electron 제거 범위, 상대 API 경로, runtime parser, 요청 병합과 실패 보존을
기록한다. 네 관점 리뷰와 필요한 재검토를 마친다.

- [ ] **Step 8: 커밋 승인**

제안 메시지:

```text
refactor(frontend): replace Electron shell with React web app
```

전체 포함 파일을 제시하고 사용자 승인 뒤에만 커밋한다.

---

### Task 5: PrimeReact 반응형 대시보드

**파일**

- 수정: `frontend/src/App.tsx`
- 수정: `frontend/src/main.tsx`
- 삭제: `frontend/src/components/StatusPanel.tsx`
- 삭제: `frontend/src/hooks/useBackendStatus.ts`
- 수정 또는 삭제: `frontend/src/__tests__/StatusPanel.test.tsx`
- 생성: `frontend/src/features/dashboard/DashboardPage.tsx`
- 생성: `frontend/src/features/dashboard/components/DashboardHeader.tsx`
- 생성: `frontend/src/features/dashboard/components/PeriodPicker.tsx`
- 생성: `frontend/src/features/dashboard/components/PerformanceSummary.tsx`
- 생성: `frontend/src/features/dashboard/components/ProfitChart.tsx`
- 생성: `frontend/src/features/dashboard/components/PositionsView.tsx`
- 생성: `frontend/src/features/dashboard/components/RecentTradesView.tsx`
- 생성: `frontend/src/features/dashboard/components/DashboardFeedback.tsx`
- 생성: `frontend/src/features/dashboard/format.ts`
- 생성: `frontend/src/hooks/usePullToRefresh.ts`
- 생성: `frontend/src/theme/ThemeProvider.tsx`
- 생성: `frontend/src/theme/preset.ts`
- 생성: `frontend/src/styles/tokens.css`
- 생성: `frontend/src/styles/dashboard.css`
- 생성: `frontend/src/features/dashboard/__tests__/DashboardPage.test.tsx`
- 생성: `frontend/src/hooks/__tests__/usePullToRefresh.test.ts`
- 생성: `docs/retrospectives/2026-07-26-phase7-responsive-ui.md`

**인터페이스**

- 소비: `useDashboard(): DashboardQueryState`
- 생성: `usePullToRefresh({enabled, refreshing, onRefresh, threshold: 72})`
- 생성: `ThemeProvider`의 `mode: "system" | "light" | "dark"`

- [ ] **Step 1: 화면 의미 테스트 작성**

Testing Library로 다음을 먼저 고정한다.

- mock 환경 badge와 마지막 갱신시각
- 기본 최근 30일, 7일·30일·이번 달 preset, 사용자 날짜 범위
- 확정·평가·총손익의 complete/partial/unavailable 대사
- 비용 미완전과 stale mark 경고
- 열린 포지션 없음과 거래 없음은 오류가 아닌 빈 상태
- refresh 실패 후 기존 KPI와 오류 배너 동시 유지
- 버튼 click이 `refresh` 1회 호출

- [ ] **Step 2: 반응형 표현 테스트 작성**

동일 DTO가 desktop DataTable과 mobile summary card에 같은 symbol, quantity,
P&L, timestamps를 전달하는지 컴포넌트 단위로 검증한다. CSS media query
자체보다 정보 손실이 없는지를 테스트한다.

- [ ] **Step 3: 테마 테스트 작성**

초기 localStorage 미설정이면 `prefers-color-scheme`, 수동 선택이면 저장값,
`system` 복귀 시 media query 변경을 반영하는지 검증한다. 저장 키는
`ohmystock.theme` 하나만 사용한다.

- [ ] **Step 4: 당겨서 새로고침 테스트 작성**

다음을 pointer/touch 이벤트로 검증한다.

- `scrollY !== 0`이면 비활성
- 아래 방향 72px 이상 뒤 release 시 1회 refresh
- 임계값 미만, 가로 제스처, 위 방향은 refresh 없음
- 이미 refreshing이면 중복 없음
- cancel과 unmount에서 상태·listener 정리
- 버튼은 제스처 미지원 환경에서도 항상 존재

- [ ] **Step 5: PrimeReact 화면 구현**

`DashboardPage`는 조립만 담당하고 각 영역은 DTO와 event callback만 받는다.
KPI 우선순위, tabular numeral, 의미 색상, Skeleton, 빈 상태, 경고 상태를
design token으로 구현한다. 장식용 아이콘·과한 gradient·깊은 shadow를
사용하지 않는다.

차트에는 기간별 누적 확정손익 line과 함께 스크린리더용 최신값·최저·최고
텍스트를 제공한다. 원화와 비율은 `Intl.NumberFormat("ko-KR")`, 시간은
`Asia/Seoul`로 포맷한다.

- [ ] **Step 6: 대표 viewport 수용**

브라우저 개발 서버 또는 production preview에서 최소 다음 폭을 확인한다.

```text
375×812   모바일
768×1024  태블릿
1440×900  데스크톱
```

확인 항목은 가로 넘침 없음, 44px 이상 터치 대상, 표→카드 전환, 날짜 picker
사용성, 라이트·다크 대비, pull indicator와 sticky header 충돌 없음이다.
결과 screenshot은 민감 데이터가 없는 fixture 화면만 사용한다.

- [ ] **Step 7: 프런트 전체 검증**

```bash
cd frontend
pnpm lint
pnpm test
pnpm typecheck
pnpm build
```

예상: 전부 PASS.

- [ ] **Step 8: 회고와 리뷰 패널**

PrimeReact 커스터마이징, 반응형 정보 보존, 접근성, pull gesture fallback,
fixture 시각 수용 결과를 기록한다. 네 관점 리뷰와 필요한 재검토를 마친다.

- [ ] **Step 9: 커밋 승인**

제안 메시지:

```text
feat(frontend): build responsive trading dashboard
```

전체 포함 파일을 제시하고 사용자 승인 뒤에만 커밋한다.

---

### Task 6: 프런트 컨테이너와 읽기 전용 프록시

**파일**

- 생성: `frontend/Dockerfile`
- 생성: `frontend/nginx.conf`
- 수정: `frontend/.dockerignore` 또는 생성
- 수정: `compose.yaml`
- 생성: `frontend/tests/nginx-routing.sh`
- 생성: `docs/runbooks/dashboard.md`
- 생성: `docs/retrospectives/2026-07-26-phase7-dashboard-deployment.md`

**인터페이스**

- 공개 루프백: `127.0.0.1:3000`
- 내부 upstream: `http://backend:8000`
- 허용 API: `GET /api/dashboard/overview`
- SPA fallback: `/index.html`

- [ ] **Step 1: routing contract 테스트 작성**

임시 nginx 컨테이너 또는 실행 중 compose fixture에 다음을 검증하는 shell
테스트를 작성한다.

- `/`와 임의 SPA route는 `text/html`
- `GET /api/dashboard/overview`는 backend로 전달되고 `/api` prefix 제거
- `POST /api/dashboard/overview`는 405
- `/api/trade/start`, `/api/trade/stop`, `/api/schedule/*`는 404
- backend가 503이면 HTML fallback이 아니라 JSON 503 유지
- `/ws`는 노출하지 않음

- [ ] **Step 2: production multi-stage 이미지 구현**

Node/pnpm build stage와 비root 정적 웹 서버 stage를 분리한다. build context에
`.env`, 소스맵, 테스트 fixture, Electron 잔재가 들어가지 않게 한다.
응답에 최소 CSP, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`를 설정한다.

- [ ] **Step 3: Compose 서비스 구현**

`frontend`는 `restart: unless-stopped`, backend 의존, healthcheck,
`127.0.0.1:3000:8080` 바인딩을 사용한다. backend의 기존
`127.0.0.1:8000` 바인딩은 유지한다. 대시보드 장애가 backend 재시작 조건이
되지 않게 의존 방향을 frontend→backend로만 둔다.

- [ ] **Step 4: 로컬 통합 검증**

```bash
docker compose config
docker compose build frontend
docker compose up -d frontend
docker compose ps
curl --fail --silent http://127.0.0.1:3000/
curl --fail --silent "http://127.0.0.1:3000/api/dashboard/overview"
```

backend 자동매매·scheduler 상태가 frontend 재시작 전후 동일한지도 읽기
전용 status로 확인한다. 실제 주문이나 키움 API는 호출하지 않는다.

- [ ] **Step 5: Tailscale runbook 작성**

runbook에는 현재 Tailscale CLI 버전의 공식 `tailscale serve` 문법을 실행
직전에 확인하도록 명시하고, 다음 원칙을 고정한다.

- HTTPS Serve 대상은 `http://127.0.0.1:3000`
- tailnet ACL로 운영자 기기만 접근
- backend 8000과 DB 15432를 Serve하지 않음
- `tailscale serve status` 확인과 제거 절차
- 모바일은 같은 tailnet 연결 뒤 HTTPS 주소 접속

실제 Serve 상태 변경은 사용자 승인 후 수용 단계에서만 수행한다.

- [ ] **Step 6: 회고와 리뷰 패널**

프록시 allowlist, security headers, loopback binding, 장애 격리, Compose와
runbook 검증을 기록한다. 네 관점 리뷰와 필요한 재검토를 마친다.

- [ ] **Step 7: 커밋 승인**

제안 메시지:

```text
feat(deploy): serve dashboard through private web gateway
```

전체 포함 파일을 제시하고 사용자 승인 뒤에만 커밋한다.

---

### Task 7: 아키텍처 전환 문서와 전체 수용

**파일**

- 수정: `AGENTS.md`
- 수정: `docs/architecture/system-overview.md`
- 수정: `docs/STATUS.md`
- 생성: `docs/retrospectives/2026-07-26-phase7-web-dashboard.md`

**인터페이스**

- 지속 런타임: Python/FastAPI/PostgreSQL + 별도 React 웹 컨테이너
- 외부 진입점: Tailscale Serve HTTPS → frontend

- [ ] **Step 1: 지속 문서 정합화**

Electron/localhost renderer 전제를 제거하고 다음을 세 문서에 동일하게
기록한다.

- React SPA와 backend 독립 컨테이너
- frontend만 Tailscale Serve로 공개
- backend와 DB loopback 유지
- dashboard 조회 전용과 broker 비호출
- 실시간 대신 명시적 갱신

`rg -n "Electron|호스트 네이티브|localhost REST/WebSocket" AGENTS.md
docs/architecture docs/STATUS.md` 결과를 검토해 역사 기록이 아닌 현재
불변조건의 낡은 문구를 모두 정정한다.

- [ ] **Step 2: 비라이브 전체 검증**

```bash
cd backend
uv run pytest
```

live 11건이 deselected되는 기존 정책을 확인한다.

```bash
cd frontend
pnpm lint
pnpm test
pnpm typecheck
pnpm build
```

모든 명령이 PASS해야 한다.

- [ ] **Step 3: 컨테이너 수용**

```bash
docker compose config
docker compose build backend frontend
docker compose up -d
docker compose ps
```

`/health`, 프런트 `/`, 프록시 dashboard GET, backend 직접 dashboard GET을
읽기 전용으로 확인한다. frontend만 재시작한 뒤 scheduler·Telegram·trade
상태가 유지되는지 검증한다.

- [ ] **Step 4: Tailscale PC·모바일 수용**

사용자 승인 뒤 runbook의 Serve 설정을 적용한다. 다른 Tailscale PC와
모바일에서 다음을 확인한다.

- HTTPS 접속
- 최근 30일 기본 기간
- 날짜 범위 변경
- 수동 새로고침
- 모바일 당겨서 새로고침
- 라이트·다크 전환과 저장
- tailnet 밖 접근 불가

실제 운영 데이터가 화면에 보이는 수용 증거에는 계좌 식별자나 토큰을
기록하지 않는다.

- [ ] **Step 5: 최종 회고와 STATUS 작성**

요청, 이전 Electron 상태, 설계 판단, 변경 파일의 정확한 위치, 테스트 결과,
리뷰 발견과 수정, Tailscale 수용 결과, 알려진 제한을 비전문가도 이해할 수
있게 기록한다. `docs/STATUS.md`의 다음 작업을 실제 운용 관찰로 갱신한다.

- [ ] **Step 6: 최종 리뷰 패널**

전체 Phase 7 diff와 배포 문서를 네 관점으로 최종 검토한다. 브로커 범위가
추가로 변경됐다면 broker-api 관점도 포함한다. Critical·Important 0과 전체
검증 재통과를 확인한다.

- [ ] **Step 7: 커밋 승인**

제안 메시지:

```text
docs(dashboard): complete responsive web dashboard
```

전체 포함 파일과 실제 검증 결과를 제시하고 사용자 승인 뒤에만 커밋한다.

---

## 완료 기준

- Tailscale 연결 PC와 모바일에서 같은 HTTPS 주소로 대시보드를 볼 수 있다.
- 초기 화면은 최근 30일의 관리 거래만 집계한다.
- 사용자 기간 선택, 버튼 새로고침, 모바일 당겨서 새로고침이 동작한다.
- 자동 폴링과 WebSocket이 없다.
- 최신 mark가 없거나 stale이면 평가손익을 확인 불가로 표시한다.
- 프런트 프록시를 통해 쓰기·WebSocket 경로에 접근할 수 없다.
- 프런트 장애·재시작이 scheduler, Telegram, 자동매매를 중단하지 않는다.
- 라이트·다크와 375px·768px·1440px 반응형 수용을 통과한다.
- backend·frontend 전체 비라이브 검증이 통과한다.
- 태스크별 회고와 리뷰 패널 Critical·Important 0을 확인한다.
