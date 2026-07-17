# 설계 Spec — Phase 0: 워킹 스켈레톤 & 기반

- **날짜:** 2026-06-16
- **상태:** 초안 (사용자 검토 대기)
- **작성:** Claude (브레인스토밍 세션)
- **범위:** OhMyStock의 첫 서브프로젝트. 레포/문서 구조, 거버넌스(CLAUDE.md), 그리고
  아키텍처 전체를 관통하는 최소 end-to-end "워킹 스켈레톤"(호스트 네이티브 Electron UI
  ↔ 컨테이너 FastAPI 백엔드 ↔ PostgreSQL)을 기능 작업 이전에 구축한다.

---

## 1. 배경 & 여기까지의 결정

브레인스토밍에서 확정된 내용(대화 참고):

1. **자산군:** 한국 **주식**.
2. **브로커:** **키움 REST API**(신규 REST/WebSocket 제품). Electron과의 크로스플랫폼
   호환을 위해 선택. 구 키움 OpenAPI+(OCX/COM)는 Windows 전용이라 OS 독립 목표와 비호환.
3. **런타임 아키텍처(A):** 컨테이너 Python(FastAPI) 백엔드 + 호스트 네이티브
   Electron/React UI. 따라서 트레이딩 엔진은 UI 종료와 무관하게 생존한다.
4. **컨테이너 경계:** 백엔드 + DB(+ 추후 Ollama)는 `docker-compose`로 실행, **Electron
   UI는 호스트에서 실행**되어 `localhost`로 접속. Electron은 데스크톱 GUI라 컨테이너에
   부적합.
5. **데이터베이스:** **PostgreSQL**(순수). 시계열 쿼리 성능이 필요하면 추후 TimescaleDB
   추가 가능.
6. **안전:** 모든 것을 **키움 모의투자**로 먼저 구축·실행.

## 2. Phase 0의 목표

**워킹 스켈레톤**을 만든다: 모든 아키텍처 계층을 관통하는 최소한이지만 실제로 동작하는
end-to-end 한 줄 + 문서/형상관리 체계. 아키텍처 리스크를 제거하고, 이후 모든 단계가
디딜 동작 토대를 제공한다.

Phase 0 **범위 밖**(명시): 실제 키움 API 호출, 데이터 수집, 스코어링, AI, 매매 로직,
프로덕션 UI. 이들은 Phase 1~8.

## 3. 구성요소 & 경계

### 3.1 `backend/` — FastAPI 서비스 (컨테이너)
- `GET /health` → 서비스 상태(`ok`), DB 연결 여부, 현재 모드(`mock`/`real`) 반환.
- WebSocket 엔드포인트(예: `/ws`) → 연결 시 hello/상태 프레임 푸시
  (`{"backend":"ok","db":"ok","mode":"mock"}`); 추후 실시간 피드의 토대.
- **설정 로더**(`core/config`): 환경변수(`.env`) 읽기 — `KIWOOM_APP_KEY`,
  `KIWOOM_SECRET_KEY`, `KIWOOM_MOCK=true`, DB DSN. 존재 검증만 하고, 키움을 호출하지는
  않는다.
- **DB 연결**(`store/`): SQLAlchemy 엔진 + 마이그레이션이 동작함을 증명하는 사소한
  마이그레이션 1개(`schema_version` 또는 `app_meta` 테이블 생성).
- **계층 구조 스텁** (CLAUDE.md §3 기준: `api/`, `core/`, `adapters/`, `domain/`,
  `store/`) — 이후 단계가 명확한 자리에 들어가도록.

### 3.2 `frontend/` — Electron + React + TS (호스트 네이티브)
- Electron 앱이 React 창을 띄운다.
- 시작 시 백엔드 `GET /health` 호출 + `/ws` WebSocket 연결.
- 단일 상태 화면 렌더링: **"Backend: ok · DB: ok · Mode: mock"**, 백엔드 미접속 시
  에러 상태도 표시.

### 3.3 `db` — PostgreSQL (컨테이너)
- 순수 `postgres` 이미지 + 영속화용 named volume.
- 자격증명/DSN은 환경변수로, 백엔드 서비스와 공유.

### 3.4 오케스트레이션 — `docker-compose.yml`
- 서비스: `db`(postgres), `backend`(fastapi). Ollama는 Phase 4에서 추가.
- `docker compose up` → 백엔드+db 빌드/기동; 백엔드는 db 헬스 대기 후 마이그레이션
  실행, 이어서 서빙.
- 프론트엔드는 호스트에서 별도 실행(`pnpm dev` / `pnpm start`).

## 4. 데이터 흐름 (Phase 0)
```
[Electron/React UI]  --HTTP /health-->  [FastAPI 백엔드]  --SQLAlchemy-->  [Postgres]
       (호스트)        --WS /ws------->     (컨테이너)                        (컨테이너)
                       <--상태 프레임--
```

## 5. 문서 & 형상관리 (규칙 1, 4, 6)
- `CLAUDE.md`(루트) — 규칙(영어) + 아키텍처 + 검증된 키움 팩트. (생성 완료)
- `docs/architecture/system-overview.md` — 마스터 청사진(Phase 0 구현 시 생성): 8개
  서브시스템, 데이터 흐름, 컨테이너 토폴로지, 일일 타임라인, 검증된 키움 팩트, 로드맵.
- `docs/plans/` — Phase 0 구현 계획서(다음 단계, writing-plans로).
- `docs/specs/` — 이 문서.
- `docs/retrospectives/` — 완료 시 Phase 0 회고록 작성(규칙 4).
- git 초기화 완료; `.gitignore`로 Python/Node/Electron/Docker 산출물 제외.
- **문서 언어:** 한국어 (CLAUDE.md만 영어 — 규칙 6).

## 6. 에러 처리
- 백엔드: 구조화 로깅; DB 다운 시 `/health`가 degraded 보고; 필수 환경변수 누락 시
  설정 로더가 명확한 메시지와 함께 즉시 실패(fail fast).
- 프론트엔드: 명시적 "백엔드 미접속" UI 상태 + 재시도; 빈 화면 금지.
- compose: 백엔드가 db 헬스체크에 의존해 DB와 경쟁(race)하지 않도록.

## 7. 테스트
- 백엔드: `pytest` — 설정 로더(누락/타입 환경변수), `/health` 응답 형태, 테스트 DB에
  마이그레이션 정상 적용.
- 프론트엔드: `vitest` — 상태 컴포넌트의 연결/에러 상태 렌더링.
- 수동 end-to-end (완료 정의): `docker compose up` → Electron 실행 → UI에
  **"Backend: ok · DB: ok · Mode: mock"** 표시. 클린 클론에서 재현 가능.

## 8. 완료 정의 (Definition of Done)
1. `docker compose up`이 `db`+`backend`를 기동; `/health`가 DB 연결됨 + `mode=mock`으로
   ok 반환.
2. 호스트 Electron 앱이 실행되어 백엔드의 연결 상태를 표시.
3. `CLAUDE.md`, `docs/architecture/system-overview.md`, 폴더 구조가 존재하고 git에
   커밋됨.
4. 백엔드·프론트엔드 단위 테스트 통과.
5. `docs/retrospectives/`에 Phase 0 회고록 존재.

## 9. 리스크 / 미해결 항목
- **키움 자격증명 & 모의투자 계정:** 사용자가 `openapi.kiwoom.com` 가입, app key/secret
  발급, 모의투자 신청 필요. Phase 1에 필요하며 Phase 0엔 불필요(Phase 0은 설정 로드만,
  라이브 호출 없음).
- **Windows에서의 Ollama GPU:** 컨테이너 Ollama는 NVIDIA + WSL2 GPU 패스스루 필요;
  대안은 `host.docker.internal`을 통한 호스트 네이티브 Ollama. Phase 4에서 결정.
- **레이트리밋 / TP-SL 클라이언트측:** Phase 0에서는 다루지 않으나 CLAUDE.md에 기록해
  Phase 1/5가 이를 고려해 설계하도록 함.
