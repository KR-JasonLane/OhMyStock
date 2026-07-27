# 아침 AI 분석 Telegram Task 4 회고 — `/analysis` 조회

## 요청과 기존 상태

- 요청은 저장된 최신 성공 아침 분석을 Telegram `/analysis`로 **조회만** 하게
  하는 것이었다. AI 재실행, broker·HTTP 호출, 주문·운영 제어 호출은 허용하지
  않았다.
- 기존 Telegram 명령은 `status`·`account`·`positions`·`help`만 query lane에
  있었고, 완료된 query intent의 응답 재구축과 unknown intent 재조정도 같은
  읽기 전용 allowlist에 한정돼 있었다.
- Task 2가 이미 `AnalysisSummaryRunStore.latest_analysis()`를 성공·완료 run
  전용 SQL read model로 제공했고, Task 1은 안전한 plain-text
  `render_analysis_summary()` presenter를 제공했다.

## 설계 판단

- `AnalysisReportQueryPort`는 `MorningAnalysisSummary | None`만 노출한다.
  `AnalysisSummaryRunStore`가 구조적으로 이를 구현하므로 domain은 SQLAlchemy
  저장소 DTO를 알지 않는다.
- `CommandProcessor`의 analysis 분기만 `asyncio.to_thread`로 해당 동기 read
  model을 호출한다. `OperationsControlPort`에는 새 메서드를 추가하지 않아
  broker·주문·scheduler·network 경로를 만들지 않았다.
- `analysis`는 InboxPoller·CommandDispatcher의 query allowlist에만 넣고,
  control allowlist에는 넣지 않았다. 따라서 query TTL/민감 outbox scrub 계약을
  유지하며 confirmation이나 control intent가 생기지 않는다.
- 완료 intent는 같은 저장 요약을 다시 표시할 수 있고, unknown intent는 조회를
  재실행하지 않은 채 성공으로 대사한다. 어느 경로도 거래 효과를 재생하지 않는다.
- 트레이딩 검토의 시점 오인 위험을 반영해 공용 presenter에 점수 기준일과 KST
  완료 시각을 넣었다. `/analysis`와 자동 요약은 같은 presenter를 사용하므로
  두 채널의 의미가 계속 일치한다.

## 변경 파일과 위치

- `backend/app/domain/notifications/models.py`: `CommandKind.ANALYSIS`를 추가했다.
- `backend/app/domain/notifications/ports.py`: 읽기 전용 `AnalysisReportQueryPort`를
  추가했다.
- `backend/app/domain/notifications/commands.py`: 포트 주입, analysis renderer,
  terminal rebuild·unknown reconciliation allowlist를 추가하고 `_call_control`을
  `_call_handler`로 이름 변경했다.
- `backend/app/core/telegram_service.py`: poller·dispatcher query lane에 analysis를
  추가했다.
- `backend/app/main.py`: 하나의 `AnalysisSummaryRunStore`를 자동 요약 서비스와
  command processor에 함께 조립했다.
- `backend/app/domain/notifications/analysis_summary.py`: 기준일과 완료 시각(KST)을
  공용 presenter에 표시했다.
- `backend/tests/notifications/test_parsing.py`, `test_commands.py`,
  `test_presentation.py`, `test_analysis_summary.py`, `test_service.py`,
  `backend/tests/test_telegram_lifespan.py`: 파싱, lane, 정상/빈 결과, terminal
  재표시, unknown 대사, 도움말, KST 시각, 자동 요약 전파를 검증했다.

## TDD·검증

- RED: 새 parser·command·help·lane 테스트를 구현 전 실행해 5개 실패를
  확인했다. 원인은 `analysis` enum/주입 포트/lane/help 부재였다.
- GREEN: 최소 구현 뒤 같은 5개 테스트가 통과했다.
- 트레이딩 패널 Important 대응도 먼저 KST 시각 표시 테스트를 추가해 실패를
  확인한 뒤 presenter를 수정했다.
- 최종 관련 비라이브 회귀와 lint/형식 검사는 아래 Task 4 보고서에 기록한다.
  실제 Telegram, 키움 API, 토큰 발급, AI 실행, broker, 주문 및 운영 DB는
  호출하지 않았다.

## 패널 리뷰

- `senior-developer`: Critical/Important 없음. 필수 회고 누락 Minor를 지적해
  이 문서로 해소했다.
- `senior-trader`: 표시 시점 오인 Important를 지적했다. 기준일·KST 완료 시각과
  테스트를 추가한 뒤 Critical/Important/Minor 없음으로 재승인했다.
- `architecture-expert`: domain port·동일 수명주기 조립·query/control lane 분리와
  read-only rebuild를 확인해 승인했다. 환경 provenance는 선행 Task 2의 장기
  Minor로 확인됐다.
- `security-expert`: 최초 환경 provenance를 Important로 제기했으나 Task 2
  direct brief와 회고의 명시적 선행 제약임을 대조한 뒤, 이번 diff는 새로
  도입·악화하지 않았다고 판단해 승인했다.

## 우려사항

- `analysis_runs`/`score_runs`에는 환경 provenance가 아직 없다. mock/real 입력이
  같은 DB에서 갈라지기 전에 migration·분석 write path·read filter·기존 행
  fail-closed를 하나의 실전 전 안전 작업으로 수행해야 한다. 이번 Task 4의
  지정 read-only 포트와 스키마 범위에는 포함하지 않았다.
