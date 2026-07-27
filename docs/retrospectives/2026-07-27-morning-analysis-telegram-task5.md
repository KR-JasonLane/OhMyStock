# 2026-07-27 — 아침 분석 Telegram Task 5 회고: `/digest` 보존 결과 조회

## 요청과 기존 상태

운영자가 `/digest`로 최근 거래 다이제스트를 조회할 때, 현재 계좌·브로커·AI·주문
상태를 다시 읽거나 과거 수치를 재계산하지 않고, 전송 outbox에 24시간 동안 보존한
payload만 다시 표시하도록 구현했다. 기존에는 장 마감 다이제스트의 생성·전송·민감
payload scrub은 있었지만, 보존된 결과를 안전하게 조회하는 명령과 read model 경계는
없었다.

## 설계 판단

- `DigestReportQueryPort`를 Task 4의 `AnalysisReportQueryPort`와 분리했다. 분석 run
  read model과 notification retention read model은 데이터 수명·환경 필터·손상 대응이
  다르므로 하나의 포트나 test double로 합치지 않는다.
- `DigestReportStore`는 `NotificationStore`의 read-only 조회만 감싼다. 조회 SQL은
  `kind='digest'`, `status='sent'`, `digest:{environment}:` idempotency prefix,
  `sensitive=true`, `purge_at > now`, payload 존재, 최소 한 delivery body 존재를
  모두 요구한다. sender는 모든 delivery part가 sent일 때만 outbox를 sent로
  종결하므로 pending 및 multipart 부분 전송은 retained 결과가 아니다.
- JSON이 object가 아닌 손상 row와 idempotency key의 환경과 payload 환경이 다른 row는
  이전 행으로 진행하되 100행에서 멈춘다. payload 환경은 `mock|real`만 허용한다. TTL
  scrub 뒤에는 payload/body가 없으므로 조회 불가 대사를 낸다. 이 제한은 손상 데이터가
  무한 scan이나 오래된 민감 정보의 복구로 이어지지 않게 한다.
- renderer는 version·환경·거래일·pipeline/trading section·account의 필수 필드와
  타입을 검증한 뒤 기존 `Digest.body` 규칙으로 렌더링한다. 저장 당시 snapshot을
  재구성할 뿐 현재 broker·계좌·AI·주문·외부 네트워크에 접근하지 않는다.
- JSON object지만 schema가 손상된 행은 명령 경계에서 `ValueError`를 조회 불가로
  fail-closed한다. 현재 store의 100행 이전 탐색 계약은 JSON object 여부와 환경
  일치까지만 대상으로 하며, schema 손상 원문이나 payload는 로그에 남기지 않는다.
- `/digest`는 poller/dispatcher query lane, 완료 intent 재표시, unknown intent
  reconciliation의 read-only allowlist에만 들어간다. 응답은 기존 민감 query outbox
  TTL/scrub 정책을 그대로 따른다.
- 원래 digest outbox는 성공 전송 후에도 24시간 payload/body를 보존한다. 단, 이 예외는
  `kind='digest'`와 `retention_kind='digest'`가 모두 맞는 행으로 한정해
  `analysis_summary`의 기존 성공 즉시 scrub을 바꾸지 않는다. TTL maintenance는
  성공 전송의 `sent` 상태·시각을 보존한 채 payload/body만 지우며, 미전송 행만
  dead-letter로 종결한다.

## 변경 파일과 위치

- `backend/app/domain/notifications/models.py`: `CommandKind.DIGEST` 추가.
- `backend/app/domain/notifications/ports.py`: 독립 `DigestReportQueryPort` 추가.
- `backend/app/domain/notifications/digest.py`: 보존 payload validator와
  `render_retained_digest` 추가.
- `backend/app/store/notification_store.py`: 환경·TTL·본문 조건의 bounded retained
  digest 조회와 `DigestReportStore` adapter 추가.
- `backend/app/domain/notifications/commands.py`,
  `backend/app/core/telegram_service.py`, `backend/app/main.py`: `/digest` 처리,
  query lane, 런타임 조립 추가.
- `backend/app/domain/notifications/presentation.py`: `/help` 조회 목록 추가.
- `backend/tests/notifications/test_digest.py`,
  `backend/tests/notifications/test_notification_store.py`,
  `backend/tests/notifications/test_commands.py`,
  `backend/tests/notifications/test_parsing.py`,
  `backend/tests/notifications/test_presentation.py`,
  `backend/tests/test_telegram_lifespan.py`: payload 재표시·손상·100행 상한·환경/TTL·
  읽기 불변·no-control·terminal/reconciliation·query lane 회귀 추가.

## 검증

- RED: renderer와 `DigestReportStore`가 아직 없는 상태에서 신규 테스트를 실행해
  import 수집 오류 3건을 확인했다.
- 독립 gate fix RED: 실제 SQLite의 2-part digest를 materialize한 직후 pending
  payload가 조회되는 실패를 확인했다. 첫 part만 sent인 동안에도 unavailable이고,
  둘째 part까지 완료되어 outbox가 sent가 된 뒤에만 payload가 반환되는 회귀로
  sender의 완전 전송 상태 계약을 고정했다.
- GREEN: gate fix까지 포함한 명세 묶음은 `226 passed`, 알림 관련 확장 묶음은
  `316 passed`, 기존
  Starlette/httpx deprecation warning 1건이었다.
- 실제 Telegram·키움 API·토큰·AI·broker·주문·운영 DB는 호출하지 않았다.

## 패널 검토

초기 패널은 environment key/payload 불일치와 optional `digest_reports` DI를
Important로 발견했다. payload 환경의 mock/real whitelist·요청 환경 exact-match skip,
필수 keyword-only DI와 명시 주입으로 수정했고 developer·trader·architecture 관점의
재검토가 승인했다. security 관점은 성공 전송 digest의 즉시 scrub, TTL 뒤 sent 감사
상태의 dead-letter 변조를 Important로 추가 발견했다. retained digest 전용 보존 예외와
sent 상태 보존 scrub 회귀로 수정한 뒤 security·developer·architecture 재검토도
Critical/Important 없음으로 승인했다. broker API 변경은 없으므로 조건부 reviewer는
실행하지 않았다.
독립 gate의 후속 Important는 pending 및 multipart 부분 전송 outbox가 retained 조회에
노출되는 문제였다. SQL에 `status='sent'`를 추가하고 실제 2-part sender 전이 회귀를
고정했다. 네 상시 reviewer의 fix 재검토는 모두 Critical/Important 없음으로 승인했다.
