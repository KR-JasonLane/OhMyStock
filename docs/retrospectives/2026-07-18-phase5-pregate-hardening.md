# 회고록 — Phase 5 사전 게이트 정비 (2026-07-18)

> 규칙 4에 따른 기록. Phase 4 마감 시 등재된 Phase 5 PRE-GATE 중 코드로
> 해소 가능한 4건(#2·#4·#5·#6)을 하나의 작업 단위로 처리했다. 계획서:
> `docs/plans/2026-07-18-phase5-pregate-hardening-plan.md`. 원장:
> `.superpowers/sdd/progress.md`.

## 1. 선행 사용자 결정 2건

- **#23 economist 파싱 실패 폴백 = 현행 유지(중립 + 상한 5, 열림).**
  트레이더 패널의 "닫힘(상한 0)" 권고를 사용자가 기각 — 파이프라인
  가용성 우선, 종목별 판정은 계속 기록되므로 감사 가능. P4 회고록 §7
  충돌 종결.
- **#24 쓰기 엔드포인트 보호 = X-API-Key 헤더 + CORS 오리진 제한.**
  토큰 미설정 시 경고만(모의 로컬 개발 편의), 실전 전환 시 필수 승격 —
  이 승격은 T3 수정 라운드에서 **코드로 강제**됐다(아래).

## 2. 태스크별 커밋과 패널 결과

| 태스크 | 구현 | 패널 수정 | 핵심 내용 |
|---|---|---|---|
| T1 분석 감사성 | `5a10a2f` | (승인, 수정 없음) | `analysis_runs.max_picks_advice` 칼럼(+Alembic `0006`), `AnalysisProgress`/`/analyze/status`에 `started_at`/`finished_at`(주입 가능한 `now` 시계), `/analyze/latest`에 `score_reference_date`(ScoreRunRow JOIN — 픽의 데이터 as-of 일자) + `max_picks_advice` 노출 |
| T2 프롬프트 비용·한계 | `8ae8ac8` | `b349bcd` | `AnalysisConfig.round_trip_cost_pct=0.25`(SSOT), 트레이더 프롬프트에 비용값 렌더링 + 백테스트 한계 2건(겹침 표본 자기상관, regime 미조건화) 명시, PROMPT_VERSION p4-v3. **패널 4인 공통 지적:** 모듈 상수(TRADER_SYSTEM, import 시 기본 cfg로 렌더)가 주입 cfg를 무시 → `AnalysisPipeline.__init__` 인스턴스별 렌더링으로 교체, prompt_hash는 "템플릿 버전 지문 — 런별 재현은 (hash, config) 쌍" 의미 확정 |
| T3 쓰기 보호 | `baaef00` | `0350323` | `require_write_token` 의존성(X-API-Key, `secrets.compare_digest` 바이트 비교) 3개 POST 부착(GET 개방), CORS `["*"]` → 오리진 allowlist(콤마 설정). **패널 수정:** `kiwoom_mock=False` + 토큰 미설정 → 기동 ValidationError(결정 #24의 승격을 코드 강제), 401 서버 경고 로그(path/reason, 값 미로그), 비ASCII 헤더 500 함정 제거, CORS≠CSRF 구분 주석, 401 e2e·GET 개방 불변식 3라우트 파라미터화 |

테스트: 299(단위 시작) → **319 passed, 10 deselected**. 패널은 T2·T3에서
Critical급 구조 결함을 각각 1건씩 잡아 수정시켰고(T2: 감사 스냅샷과 실제
LLM 입력 불일치 가능 구조, T3: 실전 전환 시 무인증 쓰기 잔존 구멍), T3
재검증에서 개발자 리뷰어는 의존성을 런타임 무력화해 파라미터화 테스트가
실제 RED가 되는 구조임을 실증했다.

## 3. 프로세스 기록

- T2 구현자가 format-at-import 설계의 한계(런타임 cfg 미반영)를 보고서에
  자진 공개 → 패널 4인이 전원 같은 지점을 지적, 수정 방향까지 수렴 —
  구현자의 정직한 리스크 공개가 리뷰 효율을 크게 높인 사례.
- T3 픽서가 caplog 테스트 간섭(alembic fileConfig의 로거 비활성화, 기존
  워크어라운드 존재)을 기존 패턴 재사용으로 해결 — 신규 패턴 발명 없음.

## 4. 잔여 PRE-GATE (Phase 5 본편/이후로)

- **#1 kt00018 실포지션 실측** — 주문 TR(kt10000) 구현이 선행돼야 모의
  포지션을 만들 수 있으므로 **Phase 5 본편 초반 태스크로 편입**(장중
  실행 필요). 라이브 테스트 기존재.
- **#7 AI 외부 추론 재평가** — 실전 전환 게이트.
- **#8 Electron 렌더러 실제 Origin 실측 후 CORS 재확정** — Phase 7 전.

## 5. 이월 carries (원장 동기)

- (트레이더) Phase 5 스펙에 주문 엔드포인트 **별도 스코프 토큰**(데이터
  트리거용과 분리) 명문화, `/score/latest`·`/analyze/latest` 무인증 신호
  조회의 실전 재평가, economist 폴백 발동 여부를 DB에서 구분할 수 있는
  필드(예: `economist_fallback` bool — 폴백 열림 유지 결정으로 중요도
  상승), 실패 런 이력 조회 GET, 보수화 문구 누적의 approve 비율 전/후
  모니터링.
- (아키텍트) `started_at`/`finished_at`·`now` 주입의 BackgroundRunService
  승격(3서비스 대칭) — **Phase 6 착수 전 필수 캡처**, Phase 5 스펙에서
  실비용 SSOT 경계(프롬프트 서술용 근사 vs 체결비용 계산) 명문화.
- (개발자) `_set` started 자동 승계 리팩터, Settings 테스트 보일러플레이트
  conftest 팩토리, P3 스코어링 스펙의 "0.2~0.3%p" 잔존 문구 정리.
