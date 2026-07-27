# Telegram 운영 절차

## 아침 AI 분석 알림과 조회

성공한 아침 AI 분석은 모의/실전 환경별로 한 번만 durable outbox에 기록되어,
**08:20 분석 성공 직후** Telegram으로 자동 전달된다. 실제 전송 시점은 sender의
재시도·Telegram 장애에 따라 조금 늦어질 수 있으며, 같은 analysis run을 다시
전송하기 위해 분석을 재실행해서는 안 된다.

전송을 마친 자동 분석 요약의 payload와 delivery body는 24시간 TTL 동안 보존하고,
TTL maintenance가 지난 뒤 scrub한다. scrub 뒤에도 sent 감사 상태는 유지한다.
`/analysis`는 이 보존본을 되살리는 명령이 아니라 성공 분석 read model을 같은
presenter로 표시하는 읽기 전용 조회다.

수집·스코어링·AI 분석은 환경 독립 공유 시장분석이다. Telegram 본문의 `알림 환경
모의투자` 또는 `알림 환경 🚨 실전`은 이 Telegram 서비스가 동작하는 broker 운영
런타임을 뜻하며, 분석 원천이 mock/real로 분리됐다는 뜻이 아니다. mock→real 전환
후에는 같은 analysis run도 환경별 outbox namespace에서 한 번씩 관찰될 수 있다.
이는 의도된 운영 분리이고, 실제 주문은 별도 거래 방어선과 실거래 승인 절차가
결정한다.

- `/analysis`: 가장 최근 성공한 AI 분석을 현재 분석·AI·거래를 다시 실행하지 않고
  읽기 전용으로 표시한다. 응답은 민감 query outbox 보존기간(TTL) 안에서만
  재시도·전달된다.
- `/digest`: 생성 당시 본문이 아직 보존된 가장 최근 16:10 거래 다이제스트를
  그대로 표시한다. 현재 계좌·브로커·주문을 재조회하거나 다이제스트를 새로
  생성하지 않는다. 본문 보존 TTL(기본 24시간)이 지난 뒤에는 조회 불가 대사가
  정상이다.

두 명령은 기존 단일 운영자 private chat 인증과 query lane을 사용한다. pause,
stop, resume, liquidate 등의 제어 명령과는 분리되어 있다.

## 자동 알림이 도착하지 않을 때

1. Telegram에서 `/status`를 먼저 조회한다. Telegram sender/outbox의 pending,
   retry, dead-letter, backoff 상태와 아침 분석 요약 루프 지연 여부만 확인한다.
2. 운영 DB와 backend 로그를 **읽기 전용**으로 확인한다. 대상 analysis run이
   `succeeded`인지, 환경별 `analysis-summary:{environment}:{run_id}` outbox가
   한 개만 있는지, delivery의 상태·재시도 사유가 무엇인지 순서대로 본다.
3. outbox가 있으면 sender의 기존 retry/lease 정책을 기다린다. outbox가 없고
   분석이 아직 성공하지 않았다면 scheduler/analysis의 실패 원인을 읽기 전용으로
   복기한다.
4. 분석 재실행, 주문, `/pause`, `/stop`, `/resume`, `/liquidate_all` 또는
   확인 명령으로 문제를 우회하지 않는다. 이들은 알림 복구 절차가 아니며 매매
   상태를 바꿀 수 있다.

`/digest`가 조회 불가인 경우에도 TTL scrub 또는 전송 미완료일 수 있다. 보존기간을
늘리거나 현재 계좌로 과거 digest를 재구성하지 말고, 해당 digest outbox와 delivery의
보존·전송 상태만 읽기 전용으로 확인한다.

## 실제 모의 Telegram 수용 승인 게이트

이 절차는 합성 SQLite E2E 검증만으로 자동 실행되지 않는다. **사용자의 별도 승인**과
다음 정상 아침 분석이 있을 때에만 모의환경에서 진행한다. 저장된 합성 run 또는 다음
정상 아침 분석을 관찰 대상으로 삼으며, 분석을 다시 실행해 인위적으로 만들지 않는다.

승인 뒤 확인할 항목은 다음 네 가지다.

1. `/analysis` 1회 송수신
2. `/digest` 1회 송수신
3. 다음 정상 08:20 분석의 자동 알림 1회
4. 동일 analysis run에 대한 중복 알림 0회

이 수용 중에도 분석 재실행, 주문, `pause`/`stop`/`resume`/`liquidate` 계열 제어
명령은 호출하지 않는다. 외부 Telegram·키움·broker·주문·운영 DB를 건드리는 행동은
승인 범위와 별도 운영 창을 다시 확인한 뒤에만 수행한다.
