# 2026-07-27 — 아침 분석 Telegram Task 6 회고: 전체 회귀와 모의 수용 준비

## 요청과 기존 상태

Task 1~5는 성공한 아침 AI 분석의 durable 자동 알림, `/analysis`, 보존된
`/digest` 조회를 구현했다. 마지막 태스크는 이 경로들을 실제 SQLite 저장소,
서비스, dispatcher, sender로 함께 검증하고 운영자가 안전하게 관찰할 절차를
마련하는 것이었다. 실제 Telegram·키움·broker·주문·분석 재실행·제어 명령은
범위 밖이며 실행하지 않았다.

## 설계 판단

- 합성 E2E는 Telegram transport만 `FakeTelegram`으로 대체했다. SQLite의
  `NotificationStore`, `AnalysisSummaryRunStore`, `DigestReportStore`, inbox,
  command store, `AnalysisSummaryService`, `CommandDispatcher`, response
  publisher, `OutboxSender`는 모두 실제 구현을 사용한다.
- seed한 성공 분석은 최종 후보 `005930 · 삼성전자`와 차순위 승인
  `000660 · SK하이닉스`를 포함한다. 자동 body literal로 분류 반전 회귀를
  잡고, 같은 service tick 및 새 service 인스턴스(재기동) 모두 outbox를 추가로
  만들지 않는지 확인한다.
- `/analysis`, `/digest`는 실제 inbox→dispatcher→publisher→sender를 지난다.
  `ForbiddenControl`은 어떤 control/account/broker 접근도 즉시 실패시켜
  조회가 외부 상태를 다시 읽지 않는 경계를 검증한다.
- collection·score·analysis run은 코드와 스키마상 환경 독립 공유 시장분석이다.
  `run_environment`은 Telegram 알림을 수행하는 broker 운영 런타임의 label과
  idempotency namespace다. 그래서 mock→real 전환은 같은 analysis run에도 각
  namespace에서 한 번의 관찰 outbox를 만들 수 있으며, 본문도 `알림 환경`으로
  그 의미를 표시한다. 분석 원천이 실전이라는 주장이 아니고 실제 주문 판단도
  하지 않는다.
- `/digest`에는 더 최신의 real digest와 mock digest를 함께 보존했다. mock query가
  real 행을 선택하지 않고 당시 mock delivery body를 part 순서까지 그대로 새 query
  outbox에 복제한다. payload는 환경·TTL·schema 무결성을 검증하는 데만 쓰며,
  과거 본문을 재렌더·재분할하지 않는다.

## 변경 파일과 위치

- `backend/tests/notifications/test_morning_analysis_telegram_e2e.py`:
  자동 분석 알림→sender, retained digest, `/analysis`·`/digest` dispatcher,
  same-tick·재기동 idempotency를 한 시나리오로 검증한다.
- `docs/runbooks/telegram.md`: 08:20 자동 알림, 조회/TTL, 미도착 읽기 전용
  복기, 실제 모의 수용 승인 게이트를 새 운영 문서로 기록했다.
- `docs/STATUS.md`: 구현 커밋·검증·미실행 외부 수용과 다음 체크포인트를 갱신했다.
- `backend/app/store/notification_store.py`,
  `backend/app/domain/notifications/{analysis_summary.py,commands.py,ports.py}`와
  `backend/app/core/telegram_service.py`: 패널 Important에 따라 비거래일 automatic
  summary 차단, summary 24시간 보존, digest 원본 part 전달, 알림 환경 표기 및
  동적 문자열 링크/명령 neutralize를 보강했다.

## RED/GREEN과 검증

- RED: retained digest 조회 SQL의 환경 prefix를 의도적으로 `digest:real:`로
  mutation하자 mock `/digest`가 `조회 가능한 최근 거래 다이제스트가 없습니다.`로
  실패했다. real/mock fixture가 environment filter 제거·오류를 실제로 잡음을
  확인했다. mutation은 즉시 원복했다.
- GREEN: 원래 환경 prefix로 복원한 뒤 합성 E2E가 통과했다. 이 테스트는
  idempotency prefix 제거 시 자동 outbox 1개 assertion, 후보/차순위 분류 반전 시
  body literal, `/digest`의 control 재조회 시 `ForbiddenControl`, digest 환경
  filter 제거 시 real/mock body assertion에서 각각 실패한다.
- 패널 RED/GREEN: 토요일 `succeeded` analysis가 자동 대상에 들어가는 실패를
  `is_trading_day()` guard로 막았다. 성공 전송 summary를 즉시 scrub하던 실패를
  24시간 보존 후 TTL scrub으로 고쳤다. `/digest`가 재렌더 header를 붙이던 E2E
  실패를 stored delivery part 재사용으로 고쳤다. URL/FQDN/IDNA/IPv4/IPv6,
  mention, slash command를 모델 문구에 넣은 presenter RED를 URL delimiter
  neutralize와 `IPv6Address` 검증으로 GREEN으로 만들었고, 종목명·`12.34%`·
  `08:20`·`1:2` 표현은 보존한다.
- 전체 비라이브 회귀는 `1367 passed, 11 deselected, 1 warning`이었다. live 11건은
  기존 정책대로 제외됐고 warning은 기존 Starlette/httpx deprecation 1건뿐이다.
  `uv run python -m compileall -q app`, `uv run alembic heads`(단일 `0015 (head)`),
  `git diff --check`도 통과했다. DB schema 변경은 없으므로 migration을 만들지
  않았다.

## 패널 검토

초기 4인 패널은 Important 다섯 가지를 발견했다: 비거래일 자동 알림, 공유
시장분석의 환경 라벨 오인, summary 즉시 scrub, `/digest` 재렌더/재분할, Telegram
자동 링크 entity 우회다. environment는 collection/score/analysis 테이블과 실행
경로가 환경 독립 공유 분석임을 코드로 확인해 설계·runbook·회고와 `알림 환경`
문구·환경별 outbox namespace 정책으로 재판정했다. 나머지는 위 RED/GREEN 코드와
회귀로 수정했다. senior-developer, senior-trader, architecture-expert,
security-expert의 최종 재검토는 모두 Critical/Important 없음으로 승인했다.

### 독립 gate 보완 — round 1

- RED: `(evil.example)`, 인용부호·emoji 경계의 host, Unicode U-label
  `예시.한국`, IDNA 가능한 `bücher.example`, `tel:`·`data:`와 `/123`·`/_hidden`을
  presenter에 넣자 기존 `://` 중심 중화로는 Telegram 자동 entity를 막지 못했다.
  bounded scheme token과 문자 TLD host token을 순차적으로 중화하고, IPv6는
  `IPv6Address` 검증을 계속 사용해 정상 `12.34%`, `08:20`, `1:2`, 한국어와 종목명을
  보존했다. 각 입력 길이는 DTO 상한(시장 500자, 항목 200자) 안이며 scheme은 32자로
  제한해 정규식 작업량도 입력 길이에 비례하도록 유지했다.
- RED: 합성 E2E의 retained `/digest`를 구별되는 두 sent delivery part로 만들고
  `CommandResponsePublisher`가 part tuple을 역순으로 전달하도록 임시 mutation하자
  dispatcher→publisher→durable sender→`FakeTelegram` 결과의 exact tuple/order
  assertion이 실패했다. 즉시 원복 후 GREEN으로, 과거 본문을 재렌더·재분할하지
  않고 순서까지 보존하는 계약을 고정했다.
- Task 2·4 회고의 환경 provenance 장기 과제는 역사 기록으로 남기되, Task 6에서
  확인한 환경 독립 공유 시장분석 정책으로 폐기·대체됐다는 후속 주석을 추가했다.
  향후 분석 입력을 환경별로 분리한다는 제품 결정이 생길 때만 migration과
  write/read filter를 별도 설계 태스크로 다시 연다.
- 보안 재검토가 괄호·인용부호 앞의 `/pause` 같은 command entity 누락을
  Important로 추가 발견했다. 원인은 문두/공백 뒤에서만 slash를 바꾸던 정규식이었다.
  `/cmd`, `/123`, `/_hidden`, `/cmd@bot` 후보를 문맥과 무관하게 중화하는 RED를
  추가하고, plain-text literal `</tag>`와 URI delimiter `//`만 보존하는 최소
  예외를 둔 뒤 GREEN으로 고쳤다. 한국어 `매수/매도`·`수익/손실`, 수치·시각
  표현과 이미 중화된 URI/FQDN은 회귀에서 보존을 확인한다.
- architecture 재검토의 Minor 두 건은 이번 gate 범위 밖의 다음 설계 작업으로
  기록했다. `latest_digest()`의 위치 기반 tuple은 이름 있는 immutable DTO로,
  `retention_kind="digest"`의 재사용은 내용 kind와 구분되는 retention policy명으로
  교체할 때 호환 migration을 함께 설계한다. Critical/Important는 없다.
- 최종 영향 재검토는 네 관점 모두 Critical/Important 없음으로 승인했다. developer는
  `</tag>`와 `//` 보존을 독립 assertion으로 더 고정할 수 있다는 Minor를, trader는
  `12.3배`·`7.5만원`·`1.2조원` 같은 한글 단위 소수가 host 중화와 겹칠 수 있다는
  Minor를 남겼다. 지정한 URL/명령 entity 안전 gate의 범위를 넘는 표현 정밀화이므로
  이번 수정에 섞지 않고 다음 presenter hardening 작업에서 분리 검토한다.

### 독립 gate 보완 — round 2

- RED: IDNA 대체점 U+3002·U+FF0E·U+FF61을 쓴 `예시。한국`,
  `evil．example`, `evil｡example`이 host 판정에서 빠졌다. 세 문자를 ASCII `.`으로
  먼저 canonicalize한 뒤 기존 bounded host 중화를 적용해 모두 `[.]`로 표시했다.
- RED: round 1의 넓은 command regex가 정상 금융·일반 표현 `P/E`, `1/2`,
  `input/output`까지 바꿨다. 앞 문자가 word·`/`·`<`·`>`이면 보존하고,
  문두·공백·괄호·인용부호·대시 뒤 `/cmd`와 `/cmd@bot`은 계속 전각 slash로
  중화하도록 경계를 좁혔다. 한국어 `매수/매도`·`수익/손실`과 URL path도
  원문 의미를 보존한다.
- RED: host 후보의 문자 포함 검사 때문에 `12.34배`, `7.5만원`, `1.2조원`이
  `[.]`로 바뀌었다. 허용한 숫자 소수+한국어 금융 단위만 정확히 보존하고,
  `127.0.0.1`, `123.한국`, 일반 FQDN은 계속 중화하는 회귀를 고정했다.
- 관련 presenter/E2E `36 passed`, 전체 비라이브 `1370 passed, 11 deselected,
  1 existing warning`을 확인했다. 네 상시 패널은 round 2 변경 범위에 새
  Critical/Important/Minor가 없다고 승인했다. round 1에서 기록한 architecture의
  immutable retained-digest DTO와 retention policy 명명 정리는 deferred Minor로
  계속 남는다.

### 독립 gate 보완 — round 3

- RED: 한국어 금융 단위 예외가 `(?:\.\d+)+`로 여러 소수부를 허용해
  `127.0.0.1원`, `127.0.0.1만원`, `1.2.3원`까지 정상 단위처럼 보존했다.
  예외를 정확히 `\d+\.\d+(배|원|만원|억원|조원)` 형태의 단일 소수로 제한했다.
- GREEN: `12.34배`, `7.5만원`, `1.2조원`은 그대로 유지하면서 위 IP/다중점
  입력은 host 경로에서 모든 점이 `[.]`로 중화된다. 관련 presenter/E2E와 전체
  회귀를 다시 실행하고 보안·트레이딩 영향 재검토를 통과했다.

### 독립 gate 보완 — round 4

- Important: `/analysis`가 저장된 성공 분석을 읽은 뒤
  `render_analysis_summary()`를 직접 호출해, 기존 command presenter의 `_present`
  예외 격리 경계를 우회했다. renderer의 예상 밖 예외가 조회 command intent
  실패로 확대될 수 있었다.
- RED/GREEN: 정상 summary를 반환하는 query fake와 `RuntimeError`를 내는 renderer를
  조합한 회귀는 기존 직접 호출에서 실패했고, `_present`를 재사용한 최소 수정 뒤
  `kind="analysis"`, `outbox_sensitive=True`, 정확한 unavailable fallback,
  succeeded intent를 반환했다. analysis 저장 조회는 한 번만 수행하며
  control/broker/AI 실행 경로는 호출하지 않는다.
- 구조화 오류 로그에는 presenter 함수명과 `exception_type=RuntimeError`만 남기고
  예외 메시지와 분석 원문은 남기지 않는 기존 계약을 회귀로 고정했다. `/digest`와
  control 명령 의미는 변경하지 않았다.
- notifications와 Telegram lifecycle 확장 회귀 `342 passed`, 전체 비라이브
  `1371 passed, 11 deselected, 1 existing warning`, `compileall`,
  `git diff --check`를 확인했다. 네 상시 패널은 round 4 변경에
  Critical/Important/Minor 없음으로 승인했다.

### 최종 broad review 보완 — round 5

- Important: `TelegramMaintenance` composite tick이 digest를 먼저 await하고
  즉시 실패를 전파해, 같은 tick의 pending analysis summary가 materialize되지
  않았다. 반대 방향인 summary failure는 이미 digest와 다른 Telegram loop를
  막지 않도록 격리돼 있어 실패 방향이 비대칭이었다.
- RED: 첫 digest `RuntimeError` 뒤 두 번째 tick을 막아 관찰하자
  `analysis_summary.calls == 0`으로 실패했다. digest 정상+cleanup 실패와
  digest+cleanup 동시 실패에서도 scheduler snapshot이 호출되지 않는 RED를
  추가해 component 결합을 재현했다.
- GREEN: digest, analysis summary, cleanup, scheduler health를 한 composite
  tick 안의 독립 예외 경계로 순서대로 실행한다. summary 오류는 기존처럼 자체
  격리하고, 나머지 오류는 발생 순서대로 수집해 첫 오류를 supervisor에
  재전달한다. 후속 오류는 component와 분류된 kind만 로그해 원문을 남기지 않는다.
- 회귀는 digest 장애 tick에도 summary materialization 1건, cleanup·scheduler
  실행, maintenance `internal_error` 관측과 다른 loop 생존, 다음 tick 복구와
  중복 materialization 0건을 고정한다. cleanup-only 오류는 그대로 전파되고,
  digest+cleanup 동시 장애는 최초 digest 오류를 유지한다.
- 관련 lifespan/service/E2E `74 passed`, 전체 비라이브
  `1374 passed, 11 deselected, 1 existing warning`, `compileall`, Alembic 단일
  `0015 (head)`, `git diff --check`를 확인했다. 최종 네 상시 패널은
  Critical/Important/Minor 없음으로 승인했다.

## 실제 수용과 다음 체크포인트

실제 Telegram 수용은 사용자 별도 승인과 다음 정상 아침 분석이 필요한 외부 단계다.
이번 태스크에서는 실제 Telegram·키움·broker·주문·분석 재실행·운영 DB·제어 명령을
호출하지 않았다. 승인 뒤 모의환경에서 `/analysis` 1회, `/digest` 1회, 다음 정상
08:20 자동 알림 1회, 동일 analysis run 중복 0회를 관찰한다. 분석 재실행이나
주문/제어 명령은 수용 방법으로 사용하지 않는다.
