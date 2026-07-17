# 마스터 아키텍처 청사진 — OhMyStock

- **작성일:** 2026-07-15
- **상태:** Phase 0 완료 (2026-07-17)
- **역할:** 이 문서는 이후 모든 Phase(1~8)의 spec이 참조하는 **마스터 청사진**이다.
  개별 Phase spec은 이 문서의 관련 절을 인용하고, 그 Phase에 국한된 세부 설계만
  추가한다.
- **근거 문서:** `CLAUDE.md`(§1·§3·§5·§6), `docs/specs/2026-06-16-phase0-walking-skeleton-design.md`(§3·§4),
  `docs/plans/2026-07-14-phase0-walking-skeleton-plan.md`(Task 6), 실제 코드
  (`backend/app/`, `frontend/src/renderer/src/`).

---

## 1. 시스템 개요

**OhMyStock**은 한국 **주식**을 대상으로 하는 자동매매 시스템이다. 시장 데이터를
수집하고, 섹터·전략별로 종목을 스코어링하며, 멀티 AI 에이전트 분석을 거쳐 매매를
자동 실행하고, 체결가를 감시해 클라이언트측에서 TP(익절)/SL(손절)/Stop을 관리한다.
모니터링·제어를 위한 데스크톱 대시보드와 텔레그램 봇을 함께 제공한다. 브로커는
**키움증권 REST API**(신규 REST/WebSocket 제품)다. 구 키움 OpenAPI+(OCX/COM)는
Windows 전용이라 크로스플랫폼 Electron UI와 호환되지 않기 때문이다.
(출처: docs/specs/2026-06-16-phase0-walking-skeleton-design.md §1)

## 2. 컨테이너 토폴로지

런타임 아키텍처는 **컨테이너 Python 백엔드 + 호스트 네이티브 Electron UI**(안 A)다.
백엔드와 DB(추후 Ollama)는 `docker-compose`로 실행하고, Electron UI는 데스크톱 GUI
특성상 컨테이너화하지 않고 호스트에서 직접 실행되어 `localhost`로 백엔드에 접속한다.
이 구도 덕분에 트레이딩 엔진은 UI 창이 닫혀도 계속 동작한다. (출처: CLAUDE.md §3,
spec §1.3~1.4)

Phase 0 계획서(Task 6)에 정의된 실제 compose 토폴로지는 다음과 같다:

```
┌─────────────────────────────┐        ┌───────────────────────────────────────┐
│   호스트 (Windows/macOS/…)   │        │              docker-compose             │
│                              │        │                                         │
│  ┌────────────────────────┐ │  HTTP  │  ┌───────────────┐     ┌─────────────┐  │
│  │  Electron + React UI    │◄┼───────►│  │   backend      │◄───►│   db         │  │
│  │  (frontend/, pnpm dev)  │ │ :8000  │  │  (FastAPI/uv)  │     │ postgres:16  │  │
│  │                          │ │  WS    │  │  포트 8000 노출 │     │ healthcheck: │  │
│  └────────────────────────┘ │        │  └───────┬───────┘     │  pg_isready  │  │
│                              │        │          │ depends_on:  └──────┬──────┘  │
│                              │        │          │ service_healthy     │         │
│                              │        │          ▼                    ▼         │
│                              │        │   alembic upgrade head   named volume    │
│                              │        │   → uvicorn 기동          "pgdata"       │
│                              │        │                                         │
│                              │        │  (Phase 4: ollama 서비스 추가 예정)       │
└─────────────────────────────┘        └───────────────────────────────────────┘
```

핵심 규칙:
- **서비스:** `db`(`postgres:16`, `POSTGRES_USER/PASSWORD/DB=ohmystock`, named volume
  `pgdata`, `healthcheck: pg_isready -U ohmystock -d ohmystock`)와 `backend`
  (`build: ./backend`, `env_file: .env`, `DATABASE_URL`을 `db` 서비스명으로 조립,
  호스트 포트 **8000:8000**).
- **기동 순서:** `backend`는 `depends_on: db (condition: service_healthy)`로 DB
  헬스체크를 기다린 뒤에만 시작해 레이스 컨디션을 방지한다.
- **자동 마이그레이션:** 백엔드 컨테이너의 `CMD`는
  `alembic upgrade head && uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000`
  — 기동 시마다 마이그레이션을 자동 적용한 뒤 서버를 띄운다.
- **프론트엔드:** compose에 포함되지 않는다. 호스트에서 `pnpm dev` / `pnpm start`로
  별도 실행되어 `http://127.0.0.1:8000`(HTTP)과 `ws://127.0.0.1:8000/ws`(WebSocket)로
  접속한다.
- **Ollama(Phase 4):** 추후 `docker-compose.yml`에 서비스로 추가하거나, Windows GPU
  패스스루 제약을 피하기 위해 `host.docker.internal`을 통해 호스트 네이티브 Ollama를
  사용하는 대안이 검토 중이다(spec §9 미해결 항목).

> ✅ **검증 상태:** 위 compose 구성은 2026-07-17 실제 `docker compose up` 기동과
> E2E DoD 검증을 마쳤다. 상세 내용은
> `docs/retrospectives/2026-07-17-phase0-walking-skeleton.md` 참고.

## 3. 백엔드 계층 구조

백엔드(`backend/app/`)는 5개 계층으로 나뉘며, 각 계층은 명확한 단일 책임을 가진다
(출처: CLAUDE.md §3, 실제 코드 `backend/app/*`):

| 계층 | 책임 | Phase 0 구현 상태 |
|---|---|---|
| `api/` | FastAPI 라우트 / WebSocket 핸들러 — **전송(transport)만** 담당, 비즈니스 로직 없음 | `health.py`(`GET /health`), `ws.py`(`WS /ws`) |
| `core/` | 설정, 로깅, 레이트리밋, 스케줄링 등 공통 기반 요소 | `config.py`(`Settings`, `get_settings`) |
| `adapters/` | 외부 연동을 포트 인터페이스 뒤에 숨김 (예: `BrokerPort` → 키움 구현체) | 빈 스텁 (Phase 1에서 채움) |
| `domain/` | 순수 비즈니스 로직 (전략, 스코어링, 매매 규칙) — 외부 I/O에 의존하지 않음 | 빈 스텁 (Phase 3·5에서 채움) |
| `store/` | 영속화 (SQLAlchemy 모델, 마이그레이션) | `db.py`(`create_db_engine`, `check_db`) + Alembic |

**확장 원칙:** 브로커는 `adapters/`의 `BrokerPort` 인터페이스 뒤에 숨긴다. 도메인
로직(스코어링·전략·매매 규칙)은 어떤 브로커를 쓰는지 알지 못하며, 오직 `BrokerPort`가
정의한 계약(주문, 시세 조회, 잔고 등)에만 의존한다. 따라서 키움을 다른 브로커(KIS
등)로 교체하더라도 `domain/`은 수정할 필요가 없다. 이는 "make-it-work-for-now"
식의 깊은 `if` 분기를 금지하고 명확한 인터페이스로 설계하라는 프로젝트 규칙(CLAUDE.md
§2 규칙 2)을 아키텍처 수준에서 구현한 것이다.

앱 진입점은 `app/main.py`의 `create_app(settings: Settings | None = None) -> FastAPI`
앱 팩토리다. 모듈 임포트만으로 환경변수를 요구하지 않도록 팩토리 함수로 분리했으며,
컨테이너에서는 `uvicorn app.main:create_app --factory`로 기동한다. settings는
`create_app()` 생성 시점에 `app.state.settings`로 설정되고, DB 엔진만 `lifespan`에서
생성/해제된다(`app.state.engine`).

## 4. 8개 서브시스템 (로드맵 Phase 1~8)

로드맵의 각 Phase는 다음 서브시스템 하나씩을 구축한다(출처: CLAUDE.md §6, §5):

1. **브로커 어댑터(Phase 1)** — `BrokerPort` 인터페이스와 키움 REST **모의투자**
   구현체. 인증(OAuth2 client_credentials, 토큰 재발급), 시세 조회, 주문 실행을
   포트 뒤에 캡슐화한다. 이후 모든 매매·데이터 관련 서브시스템이 이 포트를 통해서만
   브로커에 접근한다.
2. **데이터 수집 파이프라인(Phase 2)** — 장 마감 후 배치로 약 2,800개 전 종목의
   6개월치 일봉(`ka10081`)과 섹터 매핑을 수집한다. TR당 ~1 req/s 레이트리밋 때문에
   실시간이 아닌 **야간 배치**로 설계된다.
3. **스코어링 엔진(Phase 3)** — 자정 배치로 섹터별·전략별 수익률을 계산해 종목을
   스코어링한다. Phase 2가 수집한 데이터를 입력으로 사용한다.
4. **AI 멀티에이전트 분석(Phase 4)** — LangGraph 기반 economist(거시) + trader(개별
   종목) 에이전트가 Phase 3 스코어 결과를 필터링·정제한다. Ollama(컨테이너 또는 호스트
   네이티브)로 로컬 추론한다.
5. **트레이딩 엔진(Phase 5)** — AI 필터를 통과한 신호로 진입 주문을 실행하고, 키움
   REST에는 **네이티브 TP/SL/Stop이 없으므로** WebSocket 체결(`0B`) 스트림을 감시하며
   임계값 도달 시 클라이언트측에서 시장가/지정가 주문을 보내 익절·손절·스탑을
   관리한다.
6. **스케줄러/오케스트레이터(Phase 6)** — Phase 2~5를 하루 일정에 맞춰 순서대로
   구동하는 타임라인 실행기(§6 참고). 장 마감 수집 → 자정 스코어링 → 장 전 AI 분석 →
   장중 매매·모니터링의 순서를 보장한다.
7. **React/Electron 대시보드(Phase 7)** — 백엔드 REST/WebSocket을 소비하는 프로덕션
   UI. Phase 0의 `StatusPanel`/`useBackendStatus`가 초기 골격이다.
8. **텔레그램 봇(Phase 8)** — 매매 알림·상태 조회·수동 제어(중지/재개 등)를 텔레그램
   인터페이스로 제공한다.

## 5. 데이터 흐름

### 5.1 Phase 0 현재 흐름 (구현됨)

Phase 0은 실제 키움 API 호출·데이터 수집·매매 로직 없이, 상태 확인 한 줄만 관통하는
워킹 스켈레톤이다(출처: spec §4, 실제 코드 `backend/app/api/health.py`·`ws.py`,
`frontend/src/renderer/src/hooks/useBackendStatus.ts`):

```
[Electron/React UI]  --HTTP GET /health-->  [FastAPI 백엔드]  --SQLAlchemy SELECT 1-->  [Postgres]
       (호스트)        --WS  /ws---------->     (컨테이너)                                (컨테이너)
                       <--상태 프레임-------
```

- `GET /health` 응답: `{"status": "ok"|"degraded", "db": "ok"|"error", "mode": "mock"|"real"}`
- `WS /ws` 최초 프레임: `{"backend": "ok", "db": "ok"|"error", "mode": "mock"|"real"}`
  전송 후 연결 유지, 클라이언트가 끊을 때까지.
- 프론트엔드는 시작 시 `/health`를 호출하고 `/ws`에 연결하며, 연결이 끊기면 3초
  간격으로 재연결을 시도한다(`useBackendStatus.ts`).

### 5.2 완성 시 흐름 (Phase 1~6 목표)

```
[키움 REST/WS]                                          [Ollama / LangGraph]
      │  야간 배치 수집(ka10081 등, TR당 ~1req/s)                │
      ▼                                                        │
┌───────────┐   자정   ┌───────────┐  장 전  ┌───────────────┐  │
│ 데이터 수집 │ ───────► │ 스코어링   │ ──────► │ AI 멀티에이전트 │◄─┘
│ (Phase 2)  │         │ (Phase 3)  │        │ 필터 (Phase 4)  │
└───────────┘         └───────────┘         └───────┬───────┘
                                                      │ 매수/매도 후보
                                                      ▼
                                            ┌───────────────────┐
                                            │  트레이딩 엔진        │
                                            │  진입 주문 (Phase 5)  │──► [키움 REST 주문 kt10000/1]
                                            └─────────┬─────────┘
                                                       │ 체결 감시(WS 0B)
                                                       ▼
                                            ┌───────────────────┐
                                            │ 클라이언트측 TP/SL/    │──► [키움 REST 청산 주문]
                                            │ Stop 모니터링(Phase 5) │
                                            └───────────────────┘
                     ▲ 전체 타임라인 구동: 스케줄러/오케스트레이터 (Phase 6)
                     └────────────────────────────────────────────────────
[React/Electron 대시보드 (Phase 7)] ◄── REST/WS 상태·결과 조회 ──► [백엔드]
[텔레그램 봇 (Phase 8)]            ◄── 알림/제어 ──────────────► [백엔드]
```

## 6. 일일 운영 타임라인

키움 REST의 레이트리밋(TR당 ~1 req/s, 전역이 아닌 TR별)과 네이티브 TP/SL 부재라는
검증된 제약(§7 참고)이 아래 타임라인의 형태를 결정한다(출처: CLAUDE.md §5, §6 Phase
2·5·6):

1. **장 마감 후 — 데이터 수집 (야간 배치)**: 약 2,800개 전 종목의 6개월치 일봉을
   `ka10081`로 수집한다. TR당 ~1 req/s(버스트 ~2) 제한 때문에 전 종목 수집은 실시간이
   아닌 밤새 도는 배치 작업으로 설계된다. `cont_yn`/`next_key` 페이지네이션으로 6개월
   구간을 순회한다.
2. **자정 — 스코어링**: 수집된 데이터를 기반으로 섹터별·전략별 수익률을 계산해 종목을
   스코어링한다.
3. **장 전 — AI 분석**: economist + trader 멀티에이전트가 스코어링 결과를 검토·필터링해
   그날의 매매 후보를 정제한다.
4. **장 중 — 매매 및 모니터링**: 트레이딩 엔진이 필터를 통과한 신호로 진입 주문을
   실행하고, WebSocket 체결 스트림(`0B`)을 지속 감시하며 클라이언트측 TP/SL/Stop
   로직으로 청산 시점을 판단해 주문을 낸다.

이 타임라인 전체는 Phase 6의 스케줄러/오케스트레이터가 순서를 보장하며 구동한다.

## 7. 검증된 키움 REST 팩트 요약

아래 표의 원본 출처는 **`CLAUDE.md` §5(검증된 브로커 팩트)**이며, 신뢰 전 재검증
대상임을 명시한다. 상세 근거: https://openapi.kiwoom.com/guide/index ,
https://github.com/younghwan91/kiwoom-rest-api

| 항목 | 내용 |
|---|---|
| 인증 | `POST https://api.kiwoom.com/oauth2/token`, `grant_type=client_credentials` + `appkey` + `secretkey`. 토큰은 만료되므로 **재발급 로직 필수**. |
| 모의투자 URL | REST `https://mockapi.kiwoom.com`, WS `wss://mockapi.kiwoom.com:10000`. **KRX 전용.** |
| 실전 URL | REST `https://api.kiwoom.com`, WS `wss://api.kiwoom.com:10000`. 모의/실전은 플래그로 전환. |
| 캔들(봉) TR | 일봉 `ka10081`, 분봉 `ka10080`, 주봉 `ka10082`, 월봉 `ka10083`, 틱 `ka10079`. 페이지네이션은 `cont_yn` + `next_key`(6개월 이력 수집 시 반복). |
| 주문 TR | 매수 `kt10000`, 매도 `kt10001`, 정정 `kt10002`, 취소 `kt10003`. 호가구분: `00`(지정가), `03`(시장가), `05`(조건부지정가) 등 IOC/FOK·시간외 변형 포함. |
| TP/SL/Stop | REST에 **네이티브 조건부 자동주문이 없음.** 체결가를 WebSocket `0B`로 감시하다가 임계값 도달 시 **클라이언트측에서** 시장가/지정가 주문을 직접 전송해야 한다. |
| 레이트리밋 | 약 **1 req/s per TR(API ID)**, 버스트 ~2, **전역이 아닌 TR별**. 초과 시 HTTP 429. 토큰 버킷 리미터 + 429 재시도 필요. 전 종목(~2,800개) 봉 수집은 실시간이 아닌 야간 배치. |
| 실시간 WebSocket | `0B`(체결), `0D`(호가), `04`(잔고) 등. |

## 8. 로드맵과 의존 관계

로드맵 표는 CLAUDE.md §6과 동일하며, Phase 0의 현재 상태를 반영한다:

| Phase | 이름 | 의존 | 상태 |
|---|---|---|---|
| 0 | 워킹 스켈레톤 & 기반 | — | **완료** |
| 1 | 키움 브로커 어댑터(모의투자), `BrokerPort` 뒤에 구현 | 0 | 착수 전 |
| 2 | 데이터 수집 파이프라인 (장 마감 후, 6개월 일봉, 섹터 맵) | 1 | 착수 전 |
| 3 | 스코어링 엔진 (섹터별·전략별 수익률, 자정) | 2 | 착수 전 |
| 4 | AI 멀티에이전트 분석 (LangGraph + Ollama: economist + trader → 필터) | 2, 3 | 착수 전 |
| 5 | 트레이딩 엔진 (신호 진입 + 클라이언트측 TP/SL/Stop 모니터링) | 1 | 착수 전 |
| 6 | 스케줄러/오케스트레이터 (일일 타임라인) | 2–5 | 착수 전 |
| 7 | React/Electron 대시보드 | 1–6 | 착수 전 (Phase 0 골격만 존재) |
| 8 | 텔레그램 봇 | 1–6 | 착수 전 |

Phase 0 내부적으로는 백엔드 Task 1~5(패키지 뼈대, 설정 로더, DB 연결, Alembic
마이그레이션, `/health`, `/ws`)와 프론트엔드 Task 7~8(electron-vite 스캐폴드,
`StatusPanel`, `useBackendStatus`)에 이어 Task 6(Dockerfile + compose 기동 검증)과
Task 10(E2E 검증 + 회고록)까지 모두 완료되었다. 다음 단계는 Phase 1 spec
브레인스토밍이며, 키움 `openapi.kiwoom.com` 가입과 app key/secret 발급, 모의투자
신청이 선행 조건으로 블로킹되어 있다(출처: `docs/STATUS.md`).
