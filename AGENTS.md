# OhMyStock Codex 지침

## 작업 시작과 근거

- 작업을 재개할 때는 먼저 `docs/STATUS.md`를 읽고 현재 단계, 다음 작업, 결정 기록을 확인한다.
- 구현 전에 관련 사양과 기존 코드를 확인한다. 계획은 `docs/plans/`, 설계는 `docs/specs/`, 아키텍처는 `docs/architecture/`, 회고는 `docs/retrospectives/`에 둔다.
- 새 문서와 보고서는 한국어로 작성한다. 이 파일도 프로젝트의 지속 지침이 바뀌면 함께 갱신한다.
- 변경 가능성이 있거나 안전·금전 손실에 영향을 주는 사실은 권위 있는 최신 자료와 프로젝트의 실측 근거로 검증한다. 키움 API는 문서보다 저장소에 기록된 실측 결과를 우선한다.

## 구현 원칙

- 임시 땜질과 깊은 조건문 대신 명확한 추상화, 작은 책임, 명시적 인터페이스를 사용한다. SOLID와 DRY를 기계적으로 적용하지 말고 유지보수성과 확장성을 기준으로 판단한다.
- 작업은 검토 가능한 원자 단위로 나눈다. 각 구현 단위가 끝나면 비전문가도 이해할 수 있는 회고를 `docs/retrospectives/`에 작성한다. 요청, 기존 상태, 설계 판단, 변경 파일과 정확한 위치, 검증 결과를 포함한다.
- 사용자의 전제와 요청을 사실에 비추어 검토하고, 틀렸다면 근거와 함께 명확히 알린다.
- 커밋 전에는 전체 커밋 메시지와 포함 파일을 먼저 제시하고 명시적 확인을 기다린다. AI 저자 표시나 `Co-Authored-By` 트레일러를 넣지 않는다.
- 소스·테스트·문서에 자격 증명이나 실측 원문 데이터를 기록하지 않는다. `.env`와 `.superpowers/`의 민감 자료를 커밋하지 않는다.

## 아키텍처와 안전 불변조건

- 런타임은 컨테이너의 Python 3.12/FastAPI 백엔드 및 PostgreSQL과 호스트 네이티브 Electron/React/TypeScript UI로 구성한다. UI는 localhost REST/WebSocket으로 백엔드에 연결한다.
- 백엔드 계층은 `api/`(전송), `core/`(설정·로깅·공통 기반), `domain/`(외부 의존 없는 비즈니스 로직), `adapters/`(외부 연동), `store/`(영속성) 경계를 지킨다.
- 브로커 구현은 `BrokerPort` 뒤에 둔다. 벤더 응답 필드나 저장소 구현을 domain에 누출하지 않는다.
- 실거래 전환은 명시적 사용자 결정과 안전 가드가 있어야 한다. 기본 검증은 키움 모의 환경에서 수행한다.
- 키움 REST API 관련 구현·리뷰 전에는 `docs/reference/project-context.md` §5와 관련 회고 및 `.superpowers/sdd/`의 실측 근거를 확인한다. 실행 중인 백엔드와 같은 앱키로 별도 토큰을 발급하거나, 리뷰 목적으로 실제 API 호출을 하지 않는다.
- 거래 경로는 모든 의사결정과 방어선 활성화를 검색 가능한 로그로 남기고, 주문·체결·포지션 스냅샷·감사 데이터를 SQL로 분석 가능한 형태로 보존한다.
- LangSmith 텔레메트리를 활성화하지 않는다. 외부 모델로 보낼 데이터와 실거래 전환은 별도로 위험을 재검토한다.

## 도구와 검증

- 백엔드 의존성과 명령은 `backend/`에서 `uv`로 실행한다. 테스트는 `uv run pytest`, 마이그레이션은 `uv run alembic`을 사용한다.
- 프런트엔드는 `frontend/`에서 `pnpm`을 사용한다. 관련 변경에는 `pnpm lint`, `pnpm test`, `pnpm typecheck` 중 필요한 검사를 실행한다.
- 컨테이너 명령은 Compose v2 형식인 `docker compose`를 사용한다.
- 라이브 테스트와 실측 스크립트는 일반 검증에 포함하지 않는다. 자격 증명, 시장 시간, 외부 상태가 필요한 실행은 사용자 승인과 명시적 범위가 있을 때만 한다.

## 구현 후 리뷰

- 각 구현 태스크 뒤에는 `$ohmystock-review-panel` 스킬을 사용한다.
- `senior-developer`, `senior-trader`, `architecture-expert`, `security-expert` 네 에이전트가 같은 diff를 독립적으로 읽기 전용 리뷰해야 한다.
- 브로커 어댑터, TR 호출부, 주문·인증·페이지네이션 또는 PRE-GATE 스크립트를 건드린 경우 `broker-api-expert`도 추가한다.
- Critical 또는 Important 발견은 수정 후 해당 관점의 재검토를 통과해야 다음 태스크로 이동할 수 있다.

## 상세 프로젝트 지식

- 상세 아키텍처: `docs/architecture/system-overview.md`
- 현재 진행 상태: `docs/STATUS.md`
- 키움·Naver·Ollama 실측 사실과 로드맵: `docs/reference/project-context.md` §5, §5b, §6
- 이 `AGENTS.md`가 Codex 작업 규칙의 기준이다.
