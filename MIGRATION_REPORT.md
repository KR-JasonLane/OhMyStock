# Claude Code → Codex CLI 프로젝트 마이그레이션 보고서

작성일: 2026-07-24
대상 Codex CLI: 0.145.0

## 결과

프로젝트의 이전 지침과 5개 서브에이전트를 Codex 공식 프로젝트 경로로 변환했다. 최초 마이그레이션에서는 원본을 보존했으며, 이후 2026-07-24 사용자의 명시적 요청에 따라 프로젝트 `CLAUDE.md`와 `.claude/`를 삭제했다. 상세 통합 사실은 `docs/reference/project-context.md`로 보존했다. MCP는 범위에서 제외했다.

## 원본과 대상

| 원본 | 대상 | 자동 변환 | 주요 변경 |
|---|---|---:|---|
| `CLAUDE.md` | `AGENTS.md` | 부분 | Claude 도구·유지 지시를 제거하고 프로젝트 규칙, 아키텍처, 안전·검증 명령으로 재분류 |
| `CLAUDE.md` 규칙 8/8-b | `.agents/skills/ohmystock-review-panel/SKILL.md` | 예 | 반복 리뷰 절차를 `name`/`description` frontmatter가 있는 Codex 스킬로 구조화 |
| `.claude/agents/senior-developer.md` | `.codex/agents/senior-developer.toml` | 부분 | YAML frontmatter와 본문을 TOML 필수 필드 및 multiline `developer_instructions`로 변환 |
| `.claude/agents/senior-trader.md` | `.codex/agents/senior-trader.toml` | 부분 | 동일 |
| `.claude/agents/architecture-expert.md` | `.codex/agents/architecture-expert.toml` | 부분 | 동일 |
| `.claude/agents/security-expert.md` | `.codex/agents/security-expert.toml` | 부분 | 동일 |
| `.claude/agents/broker-api-expert.md` | `.codex/agents/broker-api-expert.toml` | 부분 | 동일 |
| `.claude/settings.local.json` | 변환 안 함 | 아니요 | Claude 명령별 allow-list와 Codex 권한 모델이 직접 대응하지 않고 과도한 권한 포함 |
| 프로젝트 멀티에이전트 요구 | `.codex/config.toml` | 예 | `[agents]` 활성화, 상시 4명+선택 1명을 위한 최대 5개 하위 스레드 |

프로젝트 `CLAUDE.local.md`, `.claude/skills/`, `.claude/commands/`, `.claude/rules/`, `.claude/settings.json`, hooks는 존재하지 않았다.

## 마이그레이션 후 원본 정리

- 삭제: `CLAUDE.md`
- 삭제: `.claude/settings.local.json`
- 삭제: `.claude/agents/`의 기존 에이전트 5개와 `.claude/` 디렉터리
- 보존·이름 변경: 아키텍처, 실측 통합 사실, 로드맵을 `docs/reference/project-context.md`로 분리
- 참조 갱신: `AGENTS.md`, 리뷰 스킬, `broker-api-expert.toml`, `docs/STATUS.md`
- 사용자 전역 `~/.claude/`는 프로젝트 정리 요청의 범위가 아니므로 유지

## 지침 분류와 충돌 정리

- 모든 프로젝트에 적용할 전역 지침: 발견되지 않았다. 이 저장소의 문서화, 리뷰, 키움 검증 규칙은 모두 프로젝트 전용이다.
- 프로젝트 지침: 문서 기반 작업, 한국어 문서, 원자적 작업과 회고, 커밋 사전 확인, 계층형 아키텍처, 모의투자 우선, 텔레메트리 차단, 리뷰 패널을 `AGENTS.md`에 보존했다.
- Claude 전용: `Agent`/`Read`/`Grep`/`Glob`/`Bash`/`WebSearch`/`WebFetch` 도구명, `CLAUDE.md` 자체 유지 요구, Claude permission 표현은 제거하거나 Codex 개념으로 바꿨다.
- Codex에서 직접 사용할 수 없는 지침: Claude 명령 단위 allow-list와 모델명 `sonnet`.
- 중복: 긴 키움·Naver·Ollama 실측 사실은 `AGENTS.md`에 복제하지 않고 원본과 회고를 참조하게 했다. 기본 32KiB 지침 한도도 피한다.
- 충돌: “항상 웹 검색”은 네트워크 승인 및 오프라인 작업과 충돌할 수 있어 변경 가능하거나 고위험인 사실을 권위 있는 최신 자료로 검증하도록 좁혔다.

## 기존 Codex 설정과 병합

프로젝트 `.codex/config.toml`은 없었으므로 새로 만들었다. 사용자 전역 `~/.codex/config.toml`의 신뢰 설정과 모델 안내 상태는 수정하지 않았다. 공식 우선순위상 명령행, 프로젝트 config, 프로필, 사용자 config 순으로 적용되며 이 프로젝트 파일은 `[agents]`만 정의하므로 기존 전역 키와 충돌하지 않는다.

## 지원되지 않거나 의도적으로 변환하지 않은 항목

- Claude `model: sonnet`: Codex 모델에 임의 대응시키지 않고 부모 세션 모델을 상속한다.
- Claude 도구 allow-list: standalone agent TOML에 동일한 per-tool allow-list가 확인되지 않았다. 리뷰어에 `sandbox_mode = "read-only"`를 적용했다.
- 로컬 permission의 `sudo`, 설치 스크립트 다운로드, 특정 curl 명령: 과도하거나 OS/셸/호스트 상태에 종속되므로 이식하지 않았다.
- hooks: 원본 hooks가 없어 만들지 않았다. Codex가 hooks를 지원하더라도 추측 변환하지 않았다.
- MCP: 요청 범위에서 명시적으로 제외했다.
- 사용자 앱 상태, 세션, 기록, 캐시, 자격 증명: 지침이나 재사용 가능한 설정이 아니므로 제외했다.

## 누락·경로·명령 검사

- 존재 확인: `docs/STATUS.md`, 문서 디렉터리 4종, `docs/architecture/system-overview.md`, 키움 client/ticks 소스, `.superpowers/sdd/`, live 주문 테스트.
- PATH에 없음: `pytest`, `alembic`, 독립 `docker-compose`.
- 대응: 프로젝트 환경을 통해 `uv run pytest`, `uv run alembic`, Compose v2 `docker compose`를 사용하도록 `AGENTS.md`에 기록했다.
- 존재 확인: `uv`, `node`, `pnpm`, `docker`, `ollama`, `curl`, `tmux`, `screen`.

## 수동 확인 항목

1. 실제 계정에 허용된 Codex 모델 정책에 따라 필요하면 각 agent TOML에 `model`과 `model_reasoning_effort`를 명시한다.
2. `max_concurrent_threads_per_session = 5`가 조직 정책이나 런타임 동시성 한도보다 높으면 실제 허용 범위로 낮춘다. 낮아도 Codex는 순차 실행할 수 있다.
3. 브로커 리뷰어가 최신 공식 문서를 조회할 때는 세션 네트워크 권한과 제공 도구에 따른다. 실제 API 호출은 금지했다.
4. 실거래 전환 전에는 별도의 보안·운영 검토가 필요하다.

## 검증

- 성공: Python `tomllib`으로 프로젝트/전역 config 및 5개 agent TOML을 파싱했다.
- 성공: 각 agent의 필수 필드와 `sandbox_mode = "read-only"`를 확인했다.
- 성공: 스킬의 `name`/`description` frontmatter와 모든 참조 경로의 존재를 확인했다.
- 성공: `codex doctor`의 `config.load`가 `ok`이며 프로젝트가 Git 루트와 trusted 저장소로 인식됐다.
- 성공: `codex exec --strict-config --ephemeral --sandbox read-only`가 실제로 프로젝트 `AGENTS.md`, `ohmystock-review-panel` 스킬, 5개 커스텀 에이전트를 모두 열거했다.
- 해당 없음: 전역 `AGENTS.md`와 전역 스킬은 변환할 원본이 없어 생성하지 않았으며, 실제 세션도 전역 AGENTS가 없다고 확인했다.
- 마이그레이션 외 실패: `codex doctor` 전체 상태는 네트워크 샌드박스에서 ChatGPT endpoint에 접근하지 못한 항목과 기존 `~/.codex/memories_1.sqlite` 접근 오류 때문에 `fail`이었다. config 로드와 이번 파일들의 인식에는 영향이 없었고 기존 상태 DB를 임의 수정하지 않았다.
