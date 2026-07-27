# 아침 분석 Telegram Task 3 회고

## 요청과 기존 상태

아침에 성공한 AI 분석을 Telegram durable outbox로 자동 전달하되, 기존
Telegram의 poller·명령·projector·sender와 별도 무한 루프를 만들지 않는 것이
요청이었다. Task 1은 사람이 읽는 안전한 요약을 만들고, Task 2는 분석 run별
idempotency outbox를 저장할 수 있는 read/write 저장소를 이미 제공했다.

## 설계 판단

`AnalysisSummaryService`는 동기 read model과 notification store를
`asyncio.to_thread`로만 호출한다. 한 tick은 이미 생성된 run id를 먼저 읽고,
당일 성공 분석을 오래된 순서로 제한 수만큼 읽어 렌더된 고정 본문과 함께
outbox로 materialize한다. Telegram adapter를 의존하지 않으므로 이 단계는
네트워크 전송이나 주문을 시작하지 않는다.

이 서비스를 기존 `TelegramMaintenance` composite tick에 넣었다. 따라서
독립 task/무한 loop가 추가되지 않고, 분석 요약 실패는 composite 내부에서
격리되어 digest·정리와 sender·commands·poller를 취소하지 않는다.
summary snapshot은 본문이나 분석 원문 없이 상태, 직전 생성 건수, backoff 사유만
노출한다.

재기동 뒤에는 durable outbox의 `analysis-summary:{환경}:{run_id}` 이력이
이미 생성한 run을 제외한다. 같은 tick 또는 프로세스 간 경합도 Task 2의 unique
idempotency key가 outbox를 하나만 남기므로, 두 번째 materialize는 생성 0건으로
끝난다. 중간에 두 번째 run 저장이 실패하면 첫 outbox transaction은 되돌리지
않고, 다음 tick이 생성 이력을 다시 읽어 남은 run만 복구한다.

종료는 먼저 digest·analysis summary producer에 비차단 fence를 세운다. fence
이후 늦게 끝난 read/build는 materialize 직전 다시 검사되어 outbox를 만들지
않는다. 진행 중 producer는 final projector/sender drain 전에 join한다. 종료
deadline을 모두 쓰면 final drain으로 진입하지 않는 fail-safe이므로, 살아남은
producer write가 drain 뒤 새 메시지를 만드는 순서 역전은 피한다.

## 변경 파일과 위치

- `backend/app/core/telegram_service.py`: `AnalysisSummaryService`, composite
  maintenance 호출, root snapshot 노출을 추가했다.
- `backend/app/main.py`: Telegram 활성화 시 같은 `settings.run_environment`로
  분석 read store와 서비스를 조립했다.
- `backend/tests/notifications/test_service.py`: 정상 순서·재기동 중복 방지,
  read 실패, 부분 실패 복구, Telegram adapter 비의존성 회귀를 추가했다.
- `backend/tests/test_main.py`: lifespan 조립의 환경 전달과 네트워크 없는
  snapshot 회귀를 추가했다.
- `backend/tests/test_telegram_lifespan.py`,
  `backend/app/domain/notifications/presentation.py`: composite 실패 격리,
  producer 종료 순서, legacy snapshot 호환, 안전한 상태 라벨의 직접 연관
  회귀·표시를 보강했다.

## 검증

- RED: `cd backend && uv run pytest tests/notifications/test_service.py -k
  analysis_summary -q`는 구현 전 `AnalysisSummaryService` import 오류로
  수집에 실패했다.
- GREEN: 같은 명령은 구현 후 `4 passed`였다.
- 조립 회귀: `uv run pytest tests/notifications/test_service.py tests/test_main.py
  -q`는 `5 passed`였다.
- 기존 Telegram/digest/store 회귀: `uv run pytest tests/test_telegram_service.py
  tests/test_telegram_lifespan.py tests/notifications/test_digest.py
  tests/notifications/test_notification_store.py -q`는 `76 passed`였다.
- 최종 관련 suite는 `191 passed`였고, 전체 비라이브 `uv run pytest -q`는
  `1336 passed, 11 deselected, 1 warning`이었다.

테스트는 fake store와 no-network Telegram client만 사용했으며 실제 Telegram,
브로커, 분석 실행 또는 주문을 호출하지 않았다.

## 자체 검토와 패널

동기 저장소 호출 세 곳이 모두 `asyncio.to_thread`인지, `run_environment`가
read store와 service에 모두 같은 설정값으로 전달되는지, maintenance 외 새
무한 loop가 없는지를 확인했다. `git diff --check`도 통과했다.

4인 패널은 composite 실패 전파, producer 종료 순서, snapshot legacy 호환,
종료 fence를 Critical/Important로 지적했다. 모두 TDD로 수정·재검토했다.
`senior-developer`, `senior-trader`, `architecture-expert`, `security-expert`
최종 판정은 각각 Critical/Important 0의 승인이다. 브로커 경로는 변경하지 않아
broker-api reviewer는 적용하지 않았다.

분석/스코어 run의 환경 provenance와 비거래일 수동 분석 자동 알림은 현 Task
1·2·3의 명시 스키마·범위 밖이다. 실전 전 별도 안전 과제로 provenance 영속화/
필터와 자동 알림 거래일 정책을 재검토해야 한다.
