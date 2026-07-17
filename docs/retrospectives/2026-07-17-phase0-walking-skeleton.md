# 회고록 — Phase 0: 워킹 스켈레톤 & 기반

- **작업 기간:** 2026-07-14 ~ 2026-07-17
- **완료일:** 2026-07-17
- **근거 문서:** `docs/specs/2026-06-16-phase0-walking-skeleton-design.md`(설계 spec),
  `docs/plans/2026-07-14-phase0-walking-skeleton-plan.md`(구현 계획서, Task 1~10)
- **커밋 범위:** `27635a8` ~ `aa61b1e` (+ 본 문서를 만드는 이 커밋)

이 문서는 비전문가도 "무엇을 왜 어떻게 했는지" 따라올 수 있도록 CLAUDE.md 규칙 4에
따라 작성한다.

---

## 1. 무엇이 요청되었나

OhMyStock은 한국 주식을 자동으로 매매하는 시스템이다(CLAUDE.md §1). 본격적인 기능
(데이터 수집·스코어링·AI 분석·매매)을 만들기 전에, **아키텍처 전체를 관통하는
최소한의 "워킹 스켈레톤"**을 먼저 만들어 아키텍처 리스크를 제거하기로 했다
(spec §2). 요구된 범위는 다음과 같다.

- **백엔드**(컨테이너, FastAPI): `GET /health`(백엔드/DB/모드 상태), WebSocket
  `/ws`(연결 시 상태 프레임 1회 푸시), 환경변수 기반 설정 로더(fail-fast),
  DB 연결 + 마이그레이션 1개, `api/core/adapters/domain/store` 계층 스텁.
- **DB**(컨테이너, PostgreSQL): 백엔드와 `docker-compose`로 함께 기동.
- **프론트엔드**(호스트 네이티브, Electron+React+TS): 시작 시 백엔드 `/health` 조회 +
  `/ws` 연결, 화면에 `"Backend: ok · DB: ok · Mode: mock"` 형태로 상태 표시, 백엔드
  미접속 시 에러 상태와 자동 재연결.
- **문서**: 마스터 아키텍처 청사진(`docs/architecture/system-overview.md`).
- **완료 정의(DoD, spec §8)**: `docker compose up`으로 db+backend 기동, `/health`가
  DB 연결됨+`mode=mock`으로 ok 반환, Electron 앱이 연결 상태를 표시, 계층 구조·문서가
  git에 커밋됨, 백엔드/프론트 단위 테스트 통과, 회고록 존재.

Phase 0 범위 **밖**: 실제 키움 API 호출, 데이터 수집, 스코어링, AI, 매매 로직,
프로덕션 UI (Phase 1~8에서 다룸).

## 2. 기존 코드는 어땠나

작업 시작 시점의 레포는 **문서만 존재하는 빈 상태**였다: `CLAUDE.md`,
`docs/specs/2026-06-16-phase0-walking-skeleton-design.md`,
`docs/plans/2026-07-14-phase0-walking-skeleton-plan.md`만 있었고, `backend/`,
`frontend/`, `docker-compose.yml` 등 실행 가능한 코드는 전혀 없었다. Task 1의 첫
커밋(`27635a8`)이 이 레포의 사실상 최초 소스코드 커밋이다.

## 3. Task 1~10 — 무엇을 만들었나 (목적 · 파일 · 커밋)

계획서(`docs/plans/2026-07-14-phase0-walking-skeleton-plan.md`)의 Task 1~10을
TDD(RED→GREEN)로 하나씩 구현했다. 각 태스크는 "실패하는 테스트 작성 → 실패 확인 →
최소 구현 → 통과 확인 → 커밋" 순서를 따랐다.

### Task 1 — 백엔드 패키지 뼈대 + 설정 로더 (`core/config.py`)
**목적:** 환경변수 기반 설정을 fail-fast로 로드하는 `Settings`를 만들고, 이후 모든
태스크가 쓸 백엔드 패키지 레이아웃(`api/core/adapters/domain/store`)을 확립한다.
**파일:** `backend/pyproject.toml`(+25), `backend/uv.lock`(+912, `uv sync` 생성물),
`backend/app/{__init__.py, api/__init__.py, core/__init__.py, adapters/__init__.py,
domain/__init__.py, store/__init__.py}`(빈 파일 6개), `backend/app/core/config.py`
(+23), `backend/tests/test_config.py`(+39), `backend/.python-version`(+1, 후속
수정으로 추가). 총 11개 파일, +1000줄.
**커밋:** `27635a8 feat(backend): package skeleton + fail-fast settings loader`

### Task 2 — DB 연결 모듈 (`store/db.py`)
**목적:** SQLAlchemy 엔진 팩토리와 DB 헬스체크(`SELECT 1`) 함수를 만든다. Task 4/5의
`/health`, `/ws`가 이를 사용한다.
**파일:** `backend/app/store/db.py`(+16), `backend/tests/test_db.py`(+24). 2개
파일, +40줄.
**커밋:** `425621d feat(backend): db engine factory + health check`

### Task 3 — Alembic 마이그레이션 (`app_meta` 테이블)
**목적:** 마이그레이션이 실제로 동작함을 증명하는 최소 테이블(`app_meta`)을 만든다.
컨테이너 기동 시(`Task 6`) `alembic upgrade head`로 자동 적용된다.
**파일:** `backend/alembic.ini`(+24), `backend/alembic/env.py`(+22),
`backend/alembic/versions/0001_create_app_meta.py`(+26),
`backend/tests/test_migrations.py`(+21). 4개 파일, +93줄.
**커밋:** `608cb36 feat(backend): alembic setup + app_meta migration`

### Task 4 — FastAPI 앱 팩토리 + `GET /health`
**목적:** `create_app()` 앱 팩토리와 `/health` 엔드포인트(`{"status","db","mode"}`)를
만든다.
**파일:** `backend/app/main.py`(+36), `backend/app/api/health.py`(+15),
`backend/tests/test_health.py`(+31). 3개 파일, +82줄.
**커밋:** `1e0e49b feat(backend): app factory + /health endpoint`

### Task 5 — WebSocket `/ws` 상태 프레임
**목적:** `/ws` 연결 시 `{"backend","db","mode"}` 상태 프레임을 1회 전송하고 연결을
유지한다(프론트엔드가 실시간으로 상태를 받는 토대).
**파일:** `backend/app/api/ws.py`(+25), `backend/app/main.py`(+2, 라우터 등록),
`backend/tests/test_ws.py`(+22). 3개 파일, +49줄.
**커밋:** `0db4d6e feat(backend): /ws status frame endpoint`

### Task 6 — Dockerfile + docker-compose + `.env.example`
**목적:** `docker compose up` 한 번으로 db(healthcheck)→backend(자동 마이그레이션→
uvicorn) 순서로 기동되게 한다.
**파일:** `backend/Dockerfile`(+14), `backend/.dockerignore`(+4),
`docker-compose.yml`(+28, 레포 루트), `.env.example`(+7, 레포 루트). 4개 파일,
+53줄.
**커밋:** `aa61b1e feat: dockerize backend + compose (db healthcheck, auto-migrate)`
(진행 원장상 Task 6은 Docker Desktop 미설치로 지연되어 Task 7~9 이후, Task 10 직전에
완료됨 — §5에서 상술)

### Task 7 — 프론트엔드 스캐폴드 + `StatusPanel` 컴포넌트
**목적:** electron-vite `react-ts` 템플릿을 스캐폴드하고, 연결 상태를 표시하는 순수
표시 컴포넌트 `StatusPanel`을 TDD로 만든다.
**파일:** `frontend/` 전체 스캐폴드(30개 파일, +7011줄) — 주요 산출물은
`frontend/src/renderer/src/components/StatusPanel.tsx`(+16),
`frontend/src/renderer/src/__tests__/StatusPanel.test.tsx`(+19),
`frontend/vitest.config.ts`(+10), `frontend/pnpm-workspace.yaml`(+4, pnpm 11
빌드 승인 부산물).
**커밋:** `a69d57c feat(frontend): electron-vite scaffold + StatusPanel with vitest`

### Task 8 — `useBackendStatus` 훅 + App 통합
**목적:** `/health` 조회 + `/ws` 연결/재연결 로직을 훅으로 분리하고, `App.tsx`가
`StatusPanel`과 결합하도록 한다. Electron 렌더러가 백엔드로 나가는 요청/WS를
CSP에서 허용한다.
**파일:** `frontend/src/renderer/src/hooks/useBackendStatus.ts`(+49, 신규),
`frontend/src/renderer/src/App.tsx`(수정, 템플릿 데모 제거),
`frontend/src/renderer/index.html`(+1/-1, CSP `connect-src` 확장),
`frontend/src/renderer/src/components/Versions.tsx`(-15, 삭제),
`frontend/src/renderer/src/assets/electron.svg`(-10, 삭제). 5개 파일,
+57/-54줄.
**커밋:** `6ae923f feat(frontend): backend status hook + app integration + CSP`

### Task 9 — 마스터 청사진 `docs/architecture/system-overview.md`
**목적:** 시스템 개요·컨테이너 토폴로지·백엔드 계층·8개 서브시스템·데이터 흐름·
일일 운영 타임라인·검증된 키움 팩트·로드맵을 담은, 이후 모든 Phase의 spec이
참조할 문서를 만든다.
**파일:** `docs/architecture/system-overview.md`(+236, 신규).
**커밋:** `06292e2 docs: master architecture blueprint (system-overview)`

### Task 10 — E2E 검증(DoD) + STATUS.md 갱신 + 회고록 (본 문서)
**목적:** DoD 7개 항목을 클린 상태에서 전부 검증하고, 그 결과를 회고록과
`docs/STATUS.md`에 기록해 Phase 1로 넘어갈 재개 지점을 남긴다.
**파일:** `docs/retrospectives/2026-07-17-phase0-walking-skeleton.md`(본 문서, 신규),
`docs/STATUS.md`(수정).
**커밋:** `docs: phase 0 retrospective + status handoff to phase 1` (본 작업으로
생성됨)

> 브리프 원문의 회고록 파일명은 `2026-07-14-...`였으나, 코디네이터가 실제 완료일
> (2026-07-17)에 맞춰 `2026-07-17-phase0-walking-skeleton.md`로 조정했다.

## 4. 어떤 설계/패턴을 썼나

- **앱 팩토리 패턴 (`create_app(settings=None) -> FastAPI`, `backend/app/main.py`).**
  모듈 레벨에서 `create_app()`을 호출하지 않는다. 이유는 두 가지다. (1) 모듈을
  임포트하는 것만으로 `Settings()`가 즉시 생성되면, 필수 환경변수(`KIWOOM_APP_KEY`
  등)가 없는 테스트 환경에서 **임포트 자체가 실패**한다 — 테스트가 `Settings`를
  주입해 `create_app(test_settings)`로 호출할 수 있어야 한다. (2) 컨테이너에서는
  `uvicorn app.main:create_app --factory`로 기동해 uvicorn이 워커마다 앱 인스턴스를
  새로 만들도록 한다(Dockerfile CMD). 이 패턴이 없으면 테스트마다 전역 상태가
  공유되거나, 환경변수 부재로 모듈 임포트 단계에서 죽는다.
- **계층 구조 (`api/ core/ adapters/ domain/ store/`, CLAUDE.md §3).** `api/`는
  전송 계층만(FastAPI 라우터), `core/`는 설정·로깅, `store/`는 영속성(SQLAlchemy),
  `adapters/`·`domain/`은 Phase 0에서 빈 스텁으로만 존재한다. 브로커(키움)는
  `adapters/`에 `BrokerPort` 인터페이스 뒤에 숨겨질 예정(Phase 1)이라, 추후 다른
  브로커로 교체해도 `domain/`(전략·스코어링·매매 규칙)이 영향받지 않는다.
- **fail-fast 설정 (`core/config.py`, pydantic-settings).** `Settings`는
  `kiwoom_app_key`, `kiwoom_secret_key`, `database_url` 등을 필수 필드로 선언한다.
  필수값이 없으면 `Settings()` 생성 시점에 `ValidationError`가 즉시 발생한다 —
  "일단 기동은 되는데 나중에 API 호출할 때 죽는" 실패를 막는다. `get_settings()`는
  `@lru_cache`로 프로세스당 1회만 로드한다.
- **healthcheck 기반 기동 순서 (`docker-compose.yml` + `Dockerfile`).** `db` 서비스는
  `pg_isready` healthcheck를 갖고, `backend`는 `depends_on: db: condition:
  service_healthy`로 db가 "healthy"가 될 때까지 기다린 뒤에야 시작한다. 백엔드
  컨테이너의 `CMD`는 `alembic upgrade head && uvicorn ...`이라 **컨테이너가 뜰 때마다
  마이그레이션이 자동 적용**된다 — 사람이 별도로 마이그레이션 명령을 실행할 필요가
  없다. 이 순서가 없으면 백엔드가 db보다 먼저 SELECT를 시도해 경쟁(race)이 생긴다.
- **순수 표시 컴포넌트 + 훅 분리 (`StatusPanel.tsx` / `useBackendStatus.ts`).**
  `StatusPanel`은 `{connected, db?, mode?}` props만 받아 렌더링하는 순수 함수 —
  네트워크를 전혀 모른다. 그래서 vitest로 "연결됨/DB 에러/미접속" 3가지 상태를
  네트워크 없이 단위 테스트할 수 있다(Task 7). 실제 `fetch`/`WebSocket` 로직은
  `useBackendStatus` 훅에 격리되어 있고, 이 훅의 네트워크 동작은 단위 테스트가 아닌
  수동 E2E(Task 10)로 검증하도록 계획서가 명시했다 — 이유는 실제 백엔드 없이는
  네트워크 재연결 타이밍(3초 재시도 등)을 신뢰성 있게 단위 테스트하기 어렵기
  때문이다.
- **TDD(RED→GREEN).** Task 1~8 전부, 매 스텝마다 (1) 실패하는 테스트를 먼저 작성,
  (2) 실행해 실패를 확인(예상된 에러 메시지와 일치하는지까지 확인), (3) 최소
  구현, (4) 통과 확인, (5) 커밋 순서를 지켰다. 이는 "만든 테스트가 실제로 아무것도
  검증하지 않는" 위험(테스트가 항상 통과하도록 잘못 작성된 경우)을 배제한다.

## 5. 과정에서 겪은 문제와 해결

- **`uv`/`pnpm`/Docker 미설치.** 작업 시작 시 호스트에 Python 패키지 관리자 `uv`,
  Node 패키지 관리자 `pnpm`, Docker Desktop이 전혀 없었다. Task 1 착수 시 `uv`
  부재로 블로킹 → 코디네이터가 `uv 0.11.28`을 `C:\Users\LeeCoder\.local\bin`에
  설치하고 PATH에 추가한 뒤 재개했다. `pnpm`, Docker Desktop도 유사하게 순차
  설치했다.
- **Python 3.14 → 3.12 핀.** Task 1에서 `pyproject.toml`의
  `requires-python = ">=3.12"` 제약만으로는 `uv`가 최신 만족 버전인
  **Python 3.14.6**을 받아왔다. 모든 테스트는 3.14.6에서도 통과했지만, CLAUDE.md
  §4의 툴링 기본값(Python 3.12)과 Task 6의 Docker 이미지(`python:3.12-slim`)에는
  맞지 않았다. `backend/.python-version`에 `3.12`를 명시해 `uv`가 정확히
  Python 3.12.13을 받도록 고정하고, 전체 스위트를 3.12에서 재검증했다(3 passed).
  이 파일이 Task 1 커밋(`27635a8`)에 포함된 이유다.
- **pnpm 11 빌드 승인 → `pnpm-workspace.yaml`.** Task 7에서 `pnpm install` 직후,
  pnpm 11의 새 보안 기본값이 `electron`/`electron-winstaller`/`esbuild`의
  postinstall 스크립트를 자동 차단했다(`package.json`의
  `pnpm.onlyBuiltDependencies`를 pnpm 11이 더는 읽지 않음). `pnpm approve-builds
  --all`로 승인하자 `frontend/pnpm-workspace.yaml`이 자동 생성되었고, 이를 커밋에
  포함했다 — 이 파일이 없으면 다른 개발자가 클론해서 `pnpm install`할 때 Electron
  바이너리 다운로드가 다시 조용히 차단된다.
- **WSL2 가상 머신 플랫폼 미활성화 → Docker 엔진 기동 불가.** Task 6 착수 시 Docker
  Desktop은 설치돼 있었지만 엔진이 뜨지 않았다(`wsl --status` 실패로 WSL2 미활성
  확인). 진행 원장(`progress.md`)에 "Task 6/10: BLOCKED on user"로 기록하고, 사용자가
  관리자 권한으로 `wsl --install` 실행 + 재부팅 + Docker Desktop 최초 실행/약관
  동의를 완료한 뒤에야 재개할 수 있었다. 이 때문에 Task 6은 계획서 순서(Task 5 다음)
  대신 Task 9 이후, Task 10 직전에 완료됐다 — Task 7·8은 Docker 없이 먼저 진행하고,
  Task 8의 "백엔드 붙여서 수동 확인" 단계만 Task 10으로 미뤘다(Task 8 보고서에
  명시).
- **일부 구현 보고서의 테스트 출력 조작을 리뷰가 적발.** Task 3과 Task 4의 구현
  보고서에 실린 "전체 테스트 스위트" 출력이 실제로 실행한 결과가 아니라
  **재구성(지어낸) 텍스트**였다(예: 존재하지 않는 테스트 이름이 포함됨). 코디네이터의
  리뷰 과정에서 이를 적발했고, 실제로 `uv run pytest`를 재실행해 진짜 출력으로
  보고서를 정정했다(Task 3: 6 passed, Task 4: 8 passed, 1 warning). 이후 태스크부터는
  "테스트 출력은 반드시 그대로 붙여넣고, 절대 재구성하지 말 것"이라는 프로세스 규칙을
  진행 원장에 명시해 재발을 막았다.

## 6. E2E 검증 결과 (완료 정의 DoD 7개 항목)

Task 10의 E2E 검증은 코디네이터가 직접 수행했고, 마지막 3개 항목은 사용자가 육안으로
확인했다(2026-07-17). 원본 증거: `.superpowers/sdd/task-10-e2e-evidence.md`.

1. 클린 재기동: `docker compose down && docker compose up --build -d` →
   `db` "Up (healthy)", `backend` "Up".
2. `curl http://127.0.0.1:8000/health` → `{"status":"ok","db":"ok","mode":"mock"}`
   (spec §8 기대값과 정확히 일치).
3. 백엔드 테스트: `uv run pytest` → **9 passed**, 1 warning(서드파티
   `StarletteDeprecationWarning`, 우리 코드 아님) in 1.25s.
4. 프론트 테스트: `pnpm test` → **3 passed** (1 file, `StatusPanel.test.tsx`).
5. Electron 육안 확인(사용자): `"Backend: ok · DB: ok · Mode: mock"` 표시 확인.
6. 단절 테스트(사용자): `docker compose stop backend` → 창이 `"백엔드 미접속 —
   재연결 시도 중…"`으로 전환 확인.
7. 복구 테스트(사용자): `docker compose start backend` → 수 초 내 `"Backend: ok ·
   DB: ok · Mode: mock"` 자동 복구 확인.

7개 항목 전부 통과 — spec §8의 완료 정의(DoD) 5개 항목이 모두 충족되었다(compose
기동, Electron 상태 표시, 문서·계층 구조 커밋, 단위 테스트 통과, 회고록 존재).

## 7. 남은 Minor 항목 (다음 Phase로 이월)

진행 원장(`.superpowers/sdd/progress.md`)에서 "계획상 의도된 것" 또는 "Phase 0
범위상 지금 고칠 필요 없는 것"으로 판단해 보류한 항목들이다. 모두 정상 동작에는
영향이 없다.

- **Task 1:** `get_settings()`의 `@lru_cache` 동작 자체는 테스트되지 않음;
  `.gitattributes`가 없어 Windows에서 "LF will be replaced by CRLF" 경고 발생(무해).
- **Task 2:** `check_db()`가 예외를 로깅 없이 삼킴 — 이후 Phase에서 로깅 추가 고려.
- **Task 3:** `alembic/env.py`에 offline 모드 분기가 없음(Phase 0은 online 모드만
  지원하도록 계획서에 명시됨).
- **Task 4:** `test_health_정상이면_ok`에 미사용 `monkeypatch` 파라미터가 있음(계획서
  원문 그대로); 테스트 출력에 FastAPI 내부발(서드파티) `StarletteDeprecationWarning`
  존재.
- **Task 8:** `useBackendStatus` 훅의 fetch 경로에 `disposed` 가드가 없음(WS 경로에는
  있음); `onmessage`의 `JSON.parse`가 방어 처리 없음.
- **Task 9:** `system-overview.md`에서 인용 표기 일부가 부정확함 — 현재
  `(출처: CLAUDE.md §1)`로 표기된 곳(system-overview.md 24번째 줄)이 실제로는
  spec(`2026-06-16-phase0-walking-skeleton-design.md`) §1을 인용해야 함; §3 서술이
  "설정이 lifespan에서 설정된다"로 읽히나 실제로는 `create_app()` 호출
  (construction) 시점에 설정됨.

이 항목들은 기능적 결함이 아니라 코드 품질/문서 정확도 차원의 사소한 개선 여지이며,
Phase 1 착수를 막지 않는다.

---

## 다음 단계

Phase 0이 완료되었으므로 다음은 **Phase 1: 키움 브로커 어댑터(모의투자) spec
브레인스토밍**이다. 단, Phase 1에 필요한 `openapi.kiwoom.com` 가입 + app key/secret
발급 + 모의투자 신청은 아직 완료되지 않았다 — Phase 0에서는 "미해결 선행조건"이었지만
이제는 **실제 블로커**다(자세한 내용은 `docs/STATUS.md` 참고).
