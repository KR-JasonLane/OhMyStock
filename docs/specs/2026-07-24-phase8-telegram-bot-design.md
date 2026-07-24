# Phase 8 텔레그램 봇 설계

- 작성일: 2026-07-24
- 상태: 4인 리뷰 패널 승인
- 선행 단계: Phase 1~6
- 사용자 결정: #41~#44 및 본 문서의 설계 대화

## 1. 목적과 범위

OhMyStock은 서버에서 완전 자동으로 동작한다. 운영자는 서버 터미널을 계속
보고 있지 않아도 긴급 상태를 알아야 하며, 외부에서 안전하게 자동 실행을
멈추거나 재개할 수 있어야 한다. Phase 8은 FastAPI 백엔드에 텔레그램 봇을
내장해 다음 기능을 제공한다.

1. 진입·청산 체결, 손절, 킬스위치, 트레이딩 감시 공백, 스케줄러 중단을
   즉시 알린다.
2. 거래일 장 마감 후 당일 파이프라인·픽·손익을 한 번 요약한다.
3. 인증된 단일 운영자가 상태·계좌·포지션을 조회하고 pause/resume 및
   킬스위치를 제어할 수 있게 한다.
4. Telegram이나 네트워크 장애가 주문·감시·스케줄러를 막지 않으며,
   미전송 알림은 재부팅 후에도 복구한다.

### 1.1 포함

- 공식 Telegram Bot API의 `getUpdates` long polling과 `sendMessage`
- 단일 운영자 및 개인 채팅 인증
- 상태·계좌·포지션 조회
- 스케줄러 pause/resume
- 신규 진입 중지 및 전량 청산 킬스위치
- 위험 명령의 일회용 2단계 확인
- PostgreSQL outbox, 수신 체크포인트, 명령 감사
- 즉시 알림과 16:10 KST 장 마감 다이제스트
- Telegram 서비스 상태와 dead-letter 관측

### 1.2 제외

- webhook과 공개 인바운드 포트
- 그룹·채널·복수 운영자
- 자유문·LLM 대화
- 임의 주문, 종목 선택, 설정값 변경
- 과거 전체 기간 수익률과 입출금 보정 수익률
- 백엔드 프로세스 자체의 외부 가용성 감시
- Telegram 외 알림 채널
- 실전 전환

복수 운영자는 초기 범위에서 제외하지만 인증 판정은 운영자 식별자 컬렉션을
받는 인터페이스로 캡슐화한다. 향후 설정·저장소만 확장하고 명령 처리기를
바꾸지 않는 것이 목표다.

## 2. 확정 결정

| 번호 | 결정 | 이유 |
|---:|---|---|
| 41 | 알림과 핵심 제어를 함께 제공 | 완전 자동매매에서는 외부에서 알고 멈출 수 있어야 함 |
| 42 | 긴급 즉시 알림과 장 마감 다이제스트로 등급화 | 알림 피로와 긴급 누락을 동시에 방지 |
| 43 | 신규 Telegram 프레임워크 없이 `httpx`로 공식 Bot API 직접 호출 | 명령이 적고 기존 비공식 래퍼 회피 관례와 일치 |
| 44 | 초기에는 단일 `user_id + private chat_id` 정확 일치, 내부 인증 계약은 복수 운영자 확장 가능 | 최소 권한과 향후 확장성의 균형 |
| 45 | 백엔드 내장 서비스가 REST와 공용 제어 유스케이스를 공유 | 자기 HTTP 호출과 토큰 재전달 없이 의미론 드리프트 방지 |
| 46 | `/pause`, `/stop`은 즉시 실행하고 `/resume`, `/liquidate_all`은 2단계 확인 | 위험도에 비례한 오조작 방지 |
| 47 | 인증된 개인 채팅에 예수금·총자산·당일 실현손익·평가손익 제공 | 원격 운영에 필요한 계좌 상태를 제공하되 검증되지 않은 수익률 산식은 금지 |
| 48 | 알림은 DB outbox에 먼저 기록하고 재부팅 후 재전송 | 긴급 알림의 유실·중복 방지 |
| 49 | 장 마감 다이제스트는 거래일 16:10 KST에 1회 | 당일 거래 결과가 정리되고 19시 다음 사이클 수집 전인 시점 |

## 3. 안전 불변조건

1. Telegram API 호출은 주문·체결 감시·스케줄러 실행 경로 안에서 기다리지
   않는다.
2. Telegram 장애나 outbox 적재 실패 때문에 거래 또는 스케줄러를 중지하지
   않는다. 실패는 격리하되 검색 가능한 로그와 상태 경고를 남긴다.
3. 미인증 사용자, 그룹, 채널, forwarded message는 어떤 조회 원값이나
   제어 권한도 얻지 못한다.
4. `/resume`과 `/liquidate_all`은 유효한 1회용 확인 없이는 실행되지 않는다.
5. Telegram bot token, 키움 자격 증명, 계좌번호, 확인 토큰 원문은 로그와
   감사 데이터에 남기지 않는다.
6. 예수금·총자산·손익 원값은 인증된 Telegram 응답에만 포함하고 일반
   애플리케이션 로그와 오류 메시지에는 기록하지 않는다.
7. 계좌 조회는 실행 중인 백엔드와 같은 `BrokerPort`, `TokenManager`,
   rate limiter를 사용한다. 별도 키움 토큰을 발급하지 않는다.
8. 같은 Telegram update의 제어 부수효과와 같은 원천 이벤트·날짜의 outbox
   생성은 중복시키지 않는다. 외부 발송은 at-least-once이며 중복 시 같은
   상관 ID로 식별한다.
9. Telegram 기능은 설정이 전부 없으면 비활성이고 일부만 있으면
   fail-fast한다.
10. 일반 테스트와 replay 프로필은 실제 Telegram 또는 키움 API를 호출하지
    않는다.
11. 프로덕션은 백엔드 컨테이너 1 replica와 Uvicorn worker 1개만 허용한다.
    poller는 DB lease를 획득한 단일 소유자만 Bot API를 호출한다.

## 4. 아키텍처와 계층 경계

### 4.1 배치

Telegram 봇은 FastAPI 프로세스의 lifespan에 조립되는 내장 서비스다. 별도
컨테이너나 localhost HTTP 자기호출을 만들지 않는다. 수신 폴링, 발신 처리,
이벤트 투영은 서로 독립된 asyncio 태스크로 실행하며 trading/scheduler
태스크와 생명주기 실패를 공유하지 않는다.

백엔드 프로세스가 완전히 죽으면 내장 봇도 알릴 수 없다. Phase 8의
`scheduler dead`는 살아 있는 프로세스 안에서 스케줄러 재기동 예산이
소진된 상태를 뜻한다. 프로세스·호스트 다운 감시는 향후 외부 헬스체크의
책임이다.

### 4.2 컴포넌트

```text
app/
├── adapters/telegram/
│   └── client.py             # getUpdates/sendMessage, Telegram DTO 격리
├── api/
│   └── 기존 schedule.py/trade.py
├── core/
│   └── telegram_service.py   # polling/projector/sender 생명주기와 격리
├── domain/notifications/
│   ├── models.py             # 알림·명령·운영자·제어 결과 값 객체
│   ├── parsing.py            # 순수 명령 파싱
│   ├── authorization.py      # 운영자·채팅 인증
│   ├── commands.py           # durable 명령 유스케이스 조정
│   ├── formatting.py         # Telegram 비의존 메시지 렌더링
│   └── ports.py              # BotPort, ControlPort, NotificationStorePort
└── store/
    ├── telegram_inbox_store.py
    ├── notification_outbox_store.py
    └── telegram_command_store.py
```

구현 계획은 파일을 더 작은 책임으로 나눌 수 있지만 다음 계층과 의존
방향은 유지한다.

- `domain/notifications/`는 Telegram 응답 필드, SQLAlchemy, FastAPI를
  알지 않는다.
- Telegram 어댑터는 거래·스케줄러 객체를 알지 않는다.
- 저장소는 메시지 정책이나 명령 의미를 결정하지 않는다.
- REST API와 Telegram 명령은 동일한 공용 제어 유스케이스를 호출한다.
- Telegram 핸들러가 API 라우터 함수를 직접 호출하거나 localhost HTTP로
  자기 자신을 호출하지 않는다.

### 4.3 공용 제어 포트

공용 제어 포트는 다음 유스케이스를 노출한다.

- `system_status()`
- `account_summary()`
- `open_positions_summary()`
- `pause_scheduler()`
- `resume_scheduler(expected_state)`
- `stop_new_entries()`
- `liquidate_all(expected_state)`

REST의 기존 응답 계약은 깨지지 않는다. API 라우터에 있던 조립·검증 로직
중 Telegram과 공유해야 하는 부분만 공용 유스케이스로 옮기고 HTTP 상태
코드·FastAPI 예외 변환은 API 계층에 남긴다.

`account_summary()`는 기존 `BrokerPort.get_deposit()`과
`BrokerPort.get_balance()`를 같은 공용 account snapshot provider에서
호출한다. 당일 실현손익은 현재 run environment의 영속 거래 데이터에서
계산하고 `추정` 또는 `브로커 대사 완료` 정확도 등급을 붙인다.
평가손익·총 평가금액은 조회 시점의 브로커 잔고 응답을 우선한다. 현재
`Balance` 계약에는 계좌 전체 수익률의 검증된 분모가 없으므로 총 수익률을
임의 계산하지 않고 `제공 불가`로 표시한다. 종목별 수익률만 평균 진입가와
현재가가 모두 유효할 때 계산한다. 각 값에는 KRW 통화, KST 귀속일, 기준
시각, 출처를 함께 보존한다. 일부 소스가 실패하면 성공한 값만 반환하고
실패 필드를 명시하며 오래된 캐시를 현재값으로 가장하지 않는다.

## 5. 설정과 기동

초기 설정은 다음 세 값을 한 묶음으로 사용한다.

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_ALLOWED_USER_ID
TELEGRAM_ALLOWED_CHAT_ID
```

- 셋이 모두 없으면 Telegram 기능을 비활성화한다.
- 하나라도 있고 일부가 없으면 `Settings` 검증에서 기동을 거부한다.
- ID는 정수로 파싱하고 0 또는 음수를 거부한다.
- token은 `SecretStr`로 보관하고 오류에 원문을 포함하지 않는다.
- replay 프로필에서는 설정 유무와 관계없이 서비스를 기동하지 않는다.
- 테스트는 기본적으로 세 설정을 제거해 외부 호출을 차단한다.

실전 전환 시에는 Telegram 활성화 여부와 무관하게 기존
`API_TRADE_TOKEN`·거래 한도 게이트를 유지한다. Telegram은 안전 가드를
우회하는 별도 주문 경로가 아니다.

lifespan 종료는 다음 순서를 지킨다.

1. poller를 취소하고 이미 받은 batch의 inbox·offset 커밋을 최대 2초 기다린다.
2. 신규 command claim을 막고 실행 전 claim은 lease를 반환한다. 이미
   부수효과가 시작된 intent는 `unknown`으로 영속해 재기동 대사 대상으로
   남긴다.
3. 기존 순서대로 scheduler를 먼저 중단하고 trading의 원자 사이클 종료를
   기다린다. 이 동안 projector와 sender는 살아 있어 종료 사건을 받는다.
4. projector가 마지막 체크포인트를 커밋한 뒤 sender를 최대 5초 drain하고
   남은 delivery lease를 반환한다.

Telegram 종료 절차의 추가 대기는 총 10초를 넘기지 않는다. 강제 종료 후에는
DB inbox, command intent, operational event와 outbox lease로 복구한다.

## 6. 운영자 인증과 수신 처리

### 6.1 인증

허용 조건은 모두 참이어야 한다.

1. `message.from.id == TELEGRAM_ALLOWED_USER_ID`
2. `message.chat.id == TELEGRAM_ALLOWED_CHAT_ID`
3. `message.chat.type == "private"`
4. 지원하는 text command이며 forwarded message가 아님

인증 판정기는 `Collection[OperatorIdentity]`를 입력으로 받는다. 초기
조립은 원소 하나만 제공한다. 향후 복수 운영자 확장 시 명령 처리기를
변경하지 않는다.

미허용 update는 부수효과 없이 감사한다. 공격자에게 허용 ID, chat ID,
설정 여부를 알려주지 않는다. 응답하지 않는 것을 기본으로 하며 운영자가
허용 chat에서 잘못된 명령을 보낸 경우에만 일반적인 도움말을 반환한다.
감사와 로그의 외부 ID는 원문 대신 안정적인 keyed hash 또는 마스킹
식별자를 사용한다.

### 6.2 polling과 update 멱등성

- `getUpdates` long polling을 사용하고 webhook은 사용하지 않는다.
- Bot API origin은 코드 상수 `https://api.telegram.org`로 고정한다.
  운영 설정으로 바꿀 수 없고 테스트만 가짜 전송 객체를 주입한다. TLS
  검증은 항상 켜고 `follow_redirects=False`, `trust_env=False`로 두며
  3xx를 영구 오류로 처리한다.
- token이 URL path에 들어가는 Telegram 특성 때문에 httpx/httpcore access
  log를 활성화하지 않고 예외에는 URL 원문 대신 고정 endpoint label만
  남긴다.
- `allowed_updates=["message"]`, polling batch 최대 100을 고정한다.
  text는 256자, command token은 64자를 넘으면 파싱 전에 거부한다.
- Poller는 update를 실행하지 않고 durable inbox에 적재한다. 한 polling
  batch의 허용 update 최소행과 offset 전진은 같은 DB 트랜잭션으로
  커밋한다.
- Telegram `update_id`를 수신 멱등 키로 저장한다.
- inbox와 command intent는 상태 머신을 분리한다.
  - inbox: `received → claimed → completed|rejected`. `completed/rejected`는
    terminal이다. 부수효과 시작 전 `claimed` lease가 만료되면 `received`로
    회수한다.
  - execution intent:
    `pending → claimed → running → succeeded|failed|needs_attention`.
    세 종결 상태는 terminal이다. 실행 전 `claimed` lease 만료는 `pending`으로
    회수한다. `running` 중 worker가 사라지면 `unknown → reconciling`으로
    전이해 외부·DB 상태를 대사한 뒤 terminal로 종결하며 직접 재실행하지
    않는다.
  `owner`, lease 만료, version을 조건부 갱신해 각 행에는 한 worker만
  존재한다.
- 재기동 시 만료된 `claimed`와 모든 `received`를 먼저 스캔한다. Polling
  응답에서 이미 사라진 update도 inbox가 원천이므로 복구된다.
- 조회 명령은 별도 bounded worker로 보내 느린 broker 조회가 제어 명령을
  막지 않게 한다. 제어 명령은 한 운영자 안에서 `update_id` 순서로 처리한다.
- update 원문 전체와 임의 사용자 text는 저장하지 않는다. 명령 종류,
  인증 결과, 처리 상태, 상관 ID, 해시된 운영자 식별자만 보존한다.
- `/confirm`은 poller가 엄격한 token 문법을 확인한 뒤 원문을 즉시
  SHA-256 해시한다. inbox에는 `argument_hash`만 저장하고 원문은
  폐기한다. worker는 이 해시로 미사용 confirmation을 조건부 소비한다.
- 한 번의 polling 응답은 `update_id` 오름차순으로 처리한다.

offset을 먼저 전진시킨 뒤 처리하는 at-most-once 방식은 금지한다. DB 장애
중에는 offset을 전진시키지 않고 backoff한다. command backlog 깊이와
가장 오래된 허용 명령의 대기 시간을 상태에 노출하며, 제어 명령 처리
대기 목표는 정상 상태에서 5초 이내다.

미허용 update는 update마다 장기 행을 만들지 않는다. polling batch
트랜잭션 안에서 해시 주체별·분 단위 집계 카운터만 upsert하고 offset은
정상 전진한다. 전체 수신이 분당 300건을 넘으면 고정 비용 집계 모드로
전환하고 backlog 경고를 남긴다. 인증된 운영자 update만 개별 inbox 행으로
보존한다. 허용 update의 DB queue 상한은 1,000건이며 초과 시 polling을
backoff하고 ERROR 상태를 노출하되 이미 수신한 제어 명령을 버리지 않는다.

## 7. 명령 계약

| 명령 | 동작 | 확인 |
|---|---|---|
| `/status` | scheduler/trading/알림 서비스 상태와 포지션 수 | 없음 |
| `/account` | 예수금·총 평가금액·당일 실현손익·평가손익 | 없음 |
| `/positions` | 종목별 수량·평균 진입가·현재가·평가손익 | 없음 |
| `/pause` | scheduler 신규 트리거 중지 | 즉시 |
| `/stop` | trading 신규 진입 중지 | 즉시 |
| `/resume` | scheduler의 인메모리 pause만 해제 | 2단계 |
| `/liquidate_all` | 현재 환경의 OhMyStock 관리 포지션 전량 청산 요청 | 2단계 |
| `/confirm <token>` | 귀속된 위험 명령 실행 | 토큰 자체가 확인 |
| `/help` | 명령과 안전 의미 안내 | 없음 |

### 7.1 상태 조회

`/status`는 다음을 포함한다.

- scheduler enabled/paused/dead
- collect/score/analyze/trade의 현재 판정과 다음 예정
- trading run 상태, kill switch, 보유 포지션 수
- Telegram polling/sender 상태, pending/dead-letter 개수
- 기준 시각

계좌 금액은 `/status`에 넣지 않고 `/account`에서 명시적으로 조회한다.

### 7.2 계좌와 포지션

`/account`는 다음을 제공한다.

- 주문 가능 예수금
- 총 평가금액
- 당일 실현손익
- 보유 포지션 평가손익
- 총 수익률은 검증된 분모가 없어 제공 불가임을 표시
- 각 값의 조회 기준 시각과 실패 항목

`/positions`는 종목명·종목코드·수량·평균 진입가·현재가·평가손익·수익률을
제공한다. 계좌번호는 표시하지 않는다. 메시지 길이를 넘으면 포지션 단위로
안전하게 분할하고 각 조각에 동일한 상관 ID와 `n/N`을 표시한다. 모든
메시지는 `parse_mode` 없는 plain text로 전송해 동적 종목명·오류 문자열을
링크, mention, HTML 또는 Markdown으로 해석하지 않는다.

조회 명령은 운영자별 쿨다운과 single-flight를 적용한다. `/account`와
`/positions`의 동일 명령은 10초에 한 번만 새 broker 조회를 시작하며,
진행 중 요청은 공유한다. 10초 안의 반복 요청에는 이전 응답의 기준 시각을
명시해 재사용한다. `/status`는 broker를 호출하지 않으므로 이 쿨다운
대상이 아니다. 조회 요청은 기존 키움 rate limiter를 반드시 거치며
trading 감시·주문 요청의 우선순위를 침해하지 않아야 한다.

### 7.3 즉시 제어

`/pause`는 scheduler의 신규 잡 트리거를 중단한다. 이미 실행 중인 잡은
중단하지 않으며 이 사실을 응답에 표시한다. pause는 기존 계약대로
인메모리 상태라 프로세스 재기동 시 해제될 수 있음을 명시한다.

`/stop`은 `STOP_NEW_ENTRIES`를 요청한다. 기존 포지션 감시는 계속된다는
의미와 재기동 관련 알려진 제약을 응답에 표시한다. trading이 실행 중이
아니면 성공처럼 가장하지 않고 현재 상태를 반환한다.

`/pause`와 `/stop`도 update 처리 안에서 인메모리 메서드를 바로 호출하지
않는다. stable `update_id` 기반 command execution intent를 먼저 만들고
worker가 실행한다. `/pause`는 이미 paused면 같은 성공 결과로 수렴하고,
`/stop`은 기존 durable kill-switch mode와 현재 run ID를 대사해 같은 run에
같은 정지 요청을 반복하지 않는다. 실행 직후 크래시한 `unknown` intent는
현재 상태와 감사 DB로 성공 여부를 판정한다.

### 7.4 위험 명령 확인

`/resume`과 `/liquidate_all`은 첫 요청에서 실행하지 않는다. `/resume`은
오직 scheduler의 인메모리 pause만 해제하며 trading 킬스위치, 완료된 run,
당일 재진입 래치를 되돌리지 않는다. 중지된 trading의 재기동 명령은 Phase 8
범위에 없다. 기존 계약대로 킬스위치 완료 run은 당일 완료로 유지되고 다음
거래일에는 자동 재개될 수 있음을 응답에 표시한다.

1. 현재 상태와 예상 영향을 조회한다.
2. 암호학적으로 예측 불가능한 1회용 토큰을 발급한다.
3. 토큰 해시와 사용자·채팅·명령·상태 지문·만료 시각을 DB에 저장한다.
4. 운영자가 2분 안에 `/confirm <token>`을 보낸다.
5. 조건부 갱신으로 confirmation의 단일 승자를 정하고, 토큰 소비와
   durable command execution intent 생성을 같은 DB 트랜잭션으로 커밋한다.
6. command worker가 intent를 claim하고 상태를 다시 대사한 뒤 실행한다.

새 위험 명령 토큰을 발급하면 같은 운영자의 이전 미사용 토큰을 무효화한다.
토큰은 실행 성공 여부와 무관하게 한 번 소비하면 재사용할 수 없다. 상태가
바뀌었으면 실행하지 않고 새 조회를 요구한다.

상태 지문은 `/resume`의 경우 scheduler enabled/paused/dead,
`/liquidate_all`의 경우 현재 run ID와 열린 포지션 ID·수량의 정렬된
집합으로 만든다. 확인 시 지문이 달라지면 실행하지 않는다.

confirmation 소비와 실제 인메모리·브로커 부수효과는 단일 트랜잭션으로
묶을 수 없다. 따라서 command execution은 stable `intent_id`를 멱등 키로
공용 제어 포트에 전달한다. 재기동 시 `claimed/running/unknown` intent를
현재 scheduler 상태, 주문 DB, broker 미체결·잔고와 대사해 `succeeded`,
`failed`, `needs_attention` 중 하나로 종결한다. 같은 confirmation의
두 동시 `/confirm`은 조건부 소비에서 한 건만 intent를 만들 수 있다.

`/liquidate_all`의 사전 응답은 청산 대상 종목 수와 수량을 보여주되,
실행 가격을 보장하지 않는다. 이 명령의 `all`은 계좌 전체가 아니라 현재
run environment에서 OhMyStock이 관리하는 열린 포지션 전부를 뜻한다.
수동 매수분, 다른 시스템 보유분, DB에 없는 미관리 잔고는 자동 매도하지
않는다. `/help`, 사전 확인, 완료 응답에 이 범위를 같은 문구로 표시한다.

확인 전 broker 잔고와 DB를 대사해 관리 대상과 미관리 보유를 분리 표시한다.
미관리 보유가 있으면 완료 후에도 `계좌 전체 잔고 0 아님`을 강하게 경고한다.
계좌 전체 청산은 Phase 8 범위가 아니며 별도 명령으로 암묵 확장하지 않는다.

intent에는 확인한 포지션 ID·심볼·수량 snapshot을 보존한다. 재실행은 각
포지션의 DB 상태, broker 잔고와 기존 청산 미체결 주문을 먼저 대사하고
이미 진행 중인 매도는 재발주하지 않는다. 명령 `succeeded`는 관리 대상별
broker 잔고 0과 관련 미체결 주문 없음이 모두 확인된 경우뿐이다.
`EXIT_FAILED`, 거래정지, 장 종료, 조회 실패, 잔여 수량은
`needs_attention`으로 종결하고 종목·잔량·미체결 상태·수동 조치를 긴급
알림으로 보낸다. 확인 후에도 기존 trading 안전 가드와 협조적 정지 계약을
그대로 거친다.

## 8. 알림 원천과 투영

### 8.1 즉시 알림

| 알림 | 원천 | 중요 내용 |
|---|---|---|
| 진입 체결 | append-only `operational_events` | 주문·이번/누적 체결·잔량·가격 신뢰도 |
| 청산 체결 | append-only `operational_events` | 주문·이번/누적 체결·잔량·사유·손익 정확도 |
| 손절 | append-only `operational_events` | 손절 기준, 발주·부분/전량 체결·잔여 |
| 킬스위치 | append-only `operational_events` | 요청 모드, 접수·완료·실패 |
| trading 감시 공백 | append-only `operational_events` | `gave_up`, 마지막 실패 시각 |
| scheduler dead | 인메모리 상태 전이 | 재기동 예산 소진, 운영 조치 |
| 파이프라인 gave_up | append-only `operational_events` | 잡, 실패/포기 사유 |
| 발송기 복구 | notification 상태 | 장애 기간, pending/dead-letter |

변경되는 `trade_orders`, `trade_positions`, `trade_runs` 행을 사후 스캔해
상태 전이를 추론하지 않는다. 소유 store가 거래 상태 변경과 같은 DB
트랜잭션에서 vendor-neutral append-only `operational_events` 행을 함께
기록한다. 이 행은 Telegram 메시지가 아니라 `event_kind`, 원천 run/order/
position ID, 사건 버전, 구조화된 비민감 사실을 담는다. 거래 도메인과
store는 Telegram 어댑터나 Telegram DTO를 알지 않는다.

체결 사건은 적어도 `entry_partial_fill`, `entry_filled`,
`exit_partial_fill`, `exit_filled`, `exit_unconfirmed`,
`exit_remaining_failed`로 구분한다. 주문수량, 이번 체결수량, 누적
체결수량, 잔량, 평균체결가, `estimated|broker_reconciled` 가격 신뢰도,
미체결 주문 상태를 필수 사실로 둔다. 동일 주문의 사건 버전은 fill ID
또는 단조 증가 누적체결 버전이어야 한다. 현재 미배선인 `trade_fills`는
Phase 8에서 실제 체결 관측 경로에 연결하거나, broker가 fill 세부를 주지
않는 경로는 `unconfirmed/estimated`로 정직하게 기록한다.

Projector는 insert-only `operational_events.id` 단일 커서를 오름차순으로
읽어 `operational_event_id + notification_kind` 고유 키로 outbox를 만든다.
projector 체크포인트와 outbox insert는 같은 트랜잭션으로 커밋한다. 실패 시
같은 ID부터 재처리하고 고유 키가 중복을 제거한다. 거래 코드가 Telegram
API를 직접 호출하거나 렌더링된 메시지를 만들지 않는다.

`scheduler dead`는 현재 영속 원천이 없으므로 살아 있는 서비스의 false→true
전이를 먼저 `operational_events`에 기록한다. 기록 실패 시 메모리에 미기록
전이를 유지해 제한적으로 재시도하고 오류 로그를 남긴다. 프로세스 자체가
동시에 죽는 경우는 이 기능이 보장할 수 없다.

`SchedulerStore.record_event()`는 `Action.GAVE_UP`을 저장할 때 같은
트랜잭션에서 `scheduler_events` 행을 flush해 얻은 ID를 원천으로
`pipeline_gave_up` operational event를 함께 append한다. job이 `trade`이면
같은 사건에 `trading_monitoring_gap` 알림 종류도 투영한다. 사건 고유
버전은 `scheduler_event.id + event_kind`이며 별도 mutable `trade_runs`
스캔을 추가하지 않는다.

### 8.2 장 마감 다이제스트

거래일 16:10 KST 이후 해당 날짜의 다이제스트 outbox가 없으면 한 번
생성한다. 재기동 시 최근 7개 거래일을 조회해 미생성 날짜를 오래된 순으로
캐치업하며, 7거래일보다 오래된 누락은 `digest_skipped_stale` 감사로
종결한다. 날짜 고유 키는 outbox 생성을 중복 차단하지만 외부 전달은
at-least-once라 같은 상관 ID의 중복 메시지가 가능하다.

내용:

- 그날 거래에 사용한 수집·스코어링·분석 기준일과 성공 여부
- 후보·픽 수와 시장 국면
- 주문·진입·청산 수, 현재 보유 포지션
- 당일 실현손익과 정확도, 평가손익, 총 평가금액
- 킬스위치, gave_up, dead, dead-letter 등 주요 경고
- 19시 수집은 다음 사이클 준비 작업으로 `예정` 표시
- 데이터 기준 시각과 누락 필드

비거래일에는 정기 다이제스트를 보내지 않는다. 다이제스트도 대화형 조회와
같은 공용 account snapshot provider와 single-flight를 사용하며 낮은
우선순위와 5초 timeout을 적용한다. 실시간 조회가 실패하면 broker 재호출을
반복하지 않고 DB-only 파이프라인·거래 요약을 보내며 실패 필드를 명시한다.

## 9. outbox와 전달 보장

### 9.1 상태

```text
pending -> sending -> sent
                    \-> pending (재시도 가능)
                    \-> dead_letter (10회 또는 24시간)
```

각 outbox 행은 다음 개념을 보존한다.

- 고유 멱등 키
- 알림 종류와 중요도
- 구조화 payload
- 발생 시각과 생성 시각
- 상태, 시도 횟수, 다음 시도 시각
- lease 소유자와 만료 시각
- 성공한 Telegram message ID와 전송 시각
- 마지막 오류의 비민감 분류와 HTTP 상태

sender는 짧은 lease로 `pending`을 점유한다. 프로세스 재기동 또는 태스크
취소 후 만료된 `sending`을 `pending`으로 회수한다. 단일 인스턴스가
배포 전제지만 저장소 계약은 중복 worker에서도 같은 행을 동시에 보내지
않게 한다.

렌더링 결과가 여러 메시지이면 `notification_deliveries` child 행에
`part_index`, `total_parts`, 상태, lease, Telegram message ID를 각각
보존한다. 재시도는 아직 성공하지 않은 조각만 전송한다. 분할 결과는 최초
렌더 시 고정하며 재시도 중 순번이나 내용이 바뀌지 않는다.

Telegram은 임의 멱등 키를 받지 않으므로 HTTP 성공 응답을 DB에 기록하기
직전에 프로세스가 죽으면 동일 메시지가 한 번 더 갈 수 있는 좁은 창이
남는다. 이를 정확히 한 번 전달이라고 표현하지 않는다. DB 수준 생성은
exactly-once이고 외부 전달은 at-least-once이며, 메시지에 안정적인 사건
상관 ID를 포함해 운영자가 중복을 식별할 수 있게 한다.

### 9.2 재시도

- 네트워크 오류·timeout·Telegram 5xx: 지수 backoff + jitter
- Telegram 429: 응답의 `retry_after` 준수
- 잘못된 요청 등 영구 4xx: 즉시 dead-letter
- bot token 인증 실패: 발송기와 polling을 dead로 전환하고 무한 호출 금지
- 최대 10회 또는 최초 생성 후 24시간: dead-letter

긴급 알림이 발생 후 5분 이상 지나 전송되면 제목에 `[지연 알림]`과 실제
발생 시각을 표시한다. dead-letter는 `/status`와 로그에서 능동적으로
드러나야 한다.

긴급 이벤트 outbox 생성 목표는 영속 사건 커밋 후 5초, 첫 전송 시도 목표는
outbox 생성 후 5초다. 손절·청산 실패·trading gave_up·scheduler dead를
다이제스트와 조회 응답보다 먼저 lease한다. 목표 초과는 상태 경고와
ERROR 로그로 노출한다.

## 10. 오류 격리와 관측성

Telegram 서비스는 최소 다음 상태를 제공한다.

- enabled/disabled/dead
- polling 정상 여부와 마지막 성공 시각
- sender 정상 여부와 마지막 성공 시각
- 마지막 update ID
- pending/sending/dead-letter 수
- 마지막 비민감 오류 분류

polling, projector, sender 중 한 태스크의 예외가 trading/scheduler
태스크로 전파되면 안 된다. 각 루프는 예외 경계를 갖고, 예산 내 재기동 후
소진되면 해당 하위 상태를 dead로 바꾼다. dead 상태는 검색 가능한 ERROR
로그와 `/status`에 나타난다. 가능한 경우 남아 있는 sender가 Telegram으로
알리되, 자기 자신 전체가 죽은 경우에는 로그·상태만 보장한다.

DB outbox 적재 실패는 거래 경로를 롤백하지 않는다. Projector가 append-only
`operational_events`를 다시 읽어 복구한다. `scheduler dead` 사건 자체의
영속 실패는 메모리에 pending 상태로 유지해 재시도하고, 프로세스 동시
종료 시 유실될 수 있음을 운영 한계로 기록한다.

로그에는 명령 종류, 상관 ID, 상태 전이, 시도 횟수, 오류 분류만 남긴다.
메시지 본문, 계좌 금액, 전체 Telegram update, token, 확인 코드, 외부 ID
원문은 남기지 않는다.

## 11. 데이터 모델

다음 테이블로 책임을 분리한다. 다음 Alembic 리비전 번호는 작성 시점의
최신 `0012` 다음인 `0013`을 사용한다.

1. `telegram_updates`
   - update 멱등 키, 정규화 명령 종류, 인증/처리 상태, owner/lease/version,
     `/confirm`의 SHA-256 `argument_hash`, 상관 ID, 시각
2. `telegram_state`
   - durable polling offset, poller lease, projector 체크포인트
3. `telegram_command_executions`
   - stable intent ID, update/confirmation, expected-state 지문, 대상 snapshot,
     claim·실행·대사 결과
4. `operational_events`
   - 거래·스케줄러 변경과 같은 트랜잭션에서 기록되는 append-only 사건
5. `notification_outbox`
   - 알림 payload, lease, 재시도, 외부 전송 결과
6. `notification_deliveries`
   - outbox의 고정 메시지 조각별 상태와 Telegram message ID
7. `telegram_confirmations`
   - token hash, 명령, 운영자 귀속, 상태 지문, 만료·소비 시각
8. `telegram_command_audit`
   - 요청·거부·확인·실행 결과와 비민감 실패 분류
9. `telegram_rejected_update_counters`
   - 미허용 주체 hash·분 단위 집계와 폭주 관측치

payload는 버전 필드를 가진 구조화 JSON으로 저장한다. 렌더링된 메시지
문자열을 SSOT로 삼지 않는다. 스키마에는 원문 token, 계좌번호, 임의
사용자 text를 저장할 컬럼을 만들지 않는다. 일반 사건 outbox는 원천 ID와
렌더링에 필요한 최소 사실만 저장한다. 계좌 조회·다이제스트의 민감
payload는 `sensitive=true`로 분류하고 애플리케이션 DB 역할만 읽게 한다.
전송 성공 즉시 payload와 delivery 본문을 NULL로 지우고 비민감 결과
메타데이터만 남긴다. 조회 응답은 15분, 다이제스트는 24시간 안에 못
보내면 본문을 폐기하고 dead-letter 메타데이터만 보존한다. 백업은 기존
운영 DB와 같은 접근 통제·암호화 정책을 따른다.

`telegram_updates` 최소 메타데이터는 30일, 만료·소비된 confirmation은
90일, command audit과 outbox 전송 결과는 1년 보존한다. 미전송
`pending/sending`과 조사되지 않은 `dead_letter`는 기간 정리 대상에서
제외한다. 일 1회 정리 작업은 한 번에 삭제할 행 수를 제한해 거래 DB를
장시간 잠그지 않는다. 이 정리는 TelegramService가 소유한 maintenance
태스크가 매일 03:30 KST에 실행하며 실패는 다음 날 재시도하고
trading/scheduler에 전파하지 않는다. 별도 scheduler job으로 추가하지 않는다.

## 12. 테스트 전략

### 12.1 단위

- 허용 user/chat/private 3중 조건과 미허용 조합
- 명령 파싱, 인자 검증, 알려지지 않은 명령
- update_id 중복과 처리 상태 회수
- `/confirm` 원문은 inbox·로그에 없고 argument hash로 재처리 가능
- 확인 토큰의 해시 저장, 2분 만료, 귀속, 상태 경합, 1회 소비
- 동시 `/confirm` 두 건 중 단 하나만 command intent를 생성
- confirmation 소비 직후와 청산 실행 직후 크래시의 intent 대사
- 메시지 포맷, 길이 분할, 상관 ID, 민감 정보 비노출
- 지연 알림 판정과 다이제스트 거래일·16:10 캐치업
- 공용 제어 포트의 상태 전제와 결과

### 12.2 어댑터

실제 네트워크 없이 `respx`로 검증한다.

- `getUpdates` 정상·빈 목록·malformed 응답
- `sendMessage` 정상과 message ID
- timeout, 연결 오류, 429 `retry_after`, 5xx, 영구 4xx
- TLS 검증, redirect 미추종, 환경 프록시 미사용
- 외부 origin redirect에도 두 번째 요청이 없고 bot token이
  URL·로그·예외 문자열에 노출되지 않음
- Telegram DTO가 adapter 밖으로 누출되지 않음

### 12.3 저장소와 서비스

- outbox 고유 키와 날짜 다이제스트 중복 방지
- append-only operational event와 거래 상태 변경의 트랜잭션 원자성
- scheduler gave_up와 scheduler event의 트랜잭션 원자성 및 trade 이중 투영
- 부분체결→전량체결 사건 버전과 가격 정확도
- lease 경합과 만료 `sending` 회수
- update/command의 동시 claim 단일 승자와 poller lease
- 재시도 시각, 10회/24시간 dead-letter
- offset은 감사 커밋 이후에만 전진
- DB 오류 후 원천 재탐색으로 outbox 복구
- 분할 메시지 일부 성공 후 미전송 조각만 재시도
- 최근 7거래일 다이제스트 캐치업과 오래된 누락의 명시적 skip
- polling/projector/sender 한쪽 실패가 다른 서비스로 전파되지 않음
- 부분 설정 fail-fast, 전체 미설정 비활성, replay 강제 비활성
- REST와 Telegram이 같은 제어 포트 의미론을 사용

### 12.4 안전 회귀

- 미허용 사용자·그룹·forwarded update가 조회 원값과 제어를 얻지 못함
- 미허용 update 폭주에서 집계 비용이 유계이고 허용 `/stop` 지연 경고가 동작
- `/resume`, `/liquidate_all`이 확인 없이는 실행되지 않음
- `/resume`이 scheduler pause만 해제하고 trading kill-switch를 되돌리지 않음
- `/liquidate_all` 재처리가 기존 미체결 매도를 중복 발주하지 않음
- 청산 잔량·EXIT_FAILED가 성공이 아니라 needs_attention 긴급 알림이 됨
- Telegram 명령 폭주가 broker 감시·주문 rate limit을 고갈시키지 않음
- Telegram 장애 중 trading monitor와 scheduler tick이 정상 진행
- 로그 캡처에 bot token, 확인 token, 계좌번호, 금액 원값이 없음
- 일반 `uv run pytest`가 외부 Telegram·키움 호출을 하지 않음

### 12.5 수용 검증

전용 테스트 봇과 키움 모의 환경에서 사용자 승인 후 별도로 수행한다.

1. 허용/미허용 계정과 개인/그룹 채팅 인증
2. `/status`, `/account`, `/positions`의 값과 기준 시각
3. pause/stop 즉시 실행과 resume/liquidate 확인 흐름
4. 모의 체결·손절·킬스위치·gave_up 알림
5. Telegram 차단 후 outbox 누적, 복구 후 지연 표시 전송
6. 발송 직전·직후 프로세스 재기동과 중복 식별
7. 16:10 다이제스트 및 시각 경과 후 캐치업
8. SQL 감사와 로그 비밀·금액 비노출

수용 검증 때문에 별도 키움 토큰을 발급하거나 실전 주문을 실행하지 않는다.

## 13. 완료 기준

- 확정된 즉시 이벤트가 outbox에 중복 없이 생성된다.
- 외부 전달 성공 또는 dead-letter가 SQL로 설명 가능하다.
- 재부팅 후 polling offset, 처리 중 update, 미전송 outbox가 복구된다.
- 미허용 주체는 조회 원값과 제어 권한을 얻지 못한다.
- 위험 명령은 유효한 확인 토큰 없이는 실행되지 않는다.
- REST와 Telegram의 pause/resume·킬스위치 의미론이 일치한다.
- 거래일 다이제스트 outbox가 16:10 이후 날짜당 한 번 생성된다. 외부
  전달 중복은 같은 상관 ID로 식별된다.
- Telegram 장애가 trading/scheduler를 중단하지 않는다.
- 민감 정보가 로그·오류·감사에 남지 않는다.
- 기본 전체 테스트와 필요한 통합 테스트가 통과한다.
- 태스크별 회고와 4인 리뷰 패널의 Critical/Important 조치가 완료된다.

## 14. 운영 한계와 후속

- 내장 봇은 백엔드·호스트 전체 다운을 알릴 수 없다. 실전 전 외부
  헬스체크를 별도 검토한다.
- Bot API에는 사용자 제공 멱등 키가 없어 전송 성공과 DB 기록 사이의
  크래시 창에서 중복 알림이 가능하다. 사건 상관 ID로 식별한다.
- pause는 재기동 시 풀리는 기존 인메모리 계약이다. 응답과 도움말에
  명시하고 영속 pause는 별도 기능으로 다룬다.
- `STOP_NEW_ENTRIES` 후 크래시 시 감시 승계 문제는 Phase 6 이월
  백로그이며 Telegram이 이를 은폐하거나 우회하지 않는다.
- 복수 운영자, 역할별 권한, 별도 Telegram 서비스 분리는 실제 요구가
  생길 때 인증 컬렉션과 공용 제어 포트를 기준으로 확장한다.

## 15. 리뷰 기록

2026-07-24에 `senior-developer`, `senior-trader`,
`architecture-expert`, `security-expert`가 같은 스펙을 독립 검토했다.
키움 TR·주문 어댑터 구현 변경이 없는 설계 문서이므로
`broker-api-expert`는 추가하지 않았다.

초기 리뷰의 핵심 발견은 위험 명령 확인과 실제 제어 사이 크래시 창,
mutable 포지션 행 사후 스캔의 알림 누락, 부분체결·잔량 계약 부재,
`/resume` 의미 혼합, Bot API token URL 보안, 미허용 update 폭주,
민감 outbox 장기 보존이었다. 다음을 반영했다.

- 모든 제어 명령을 durable command intent와 재기동 대사 계약으로 통일
- append-only `operational_events`와 단일 projector cursor 도입
- 부분/전량체결·잔량·가격 정확도 및 청산 `needs_attention` 정의
- `/resume`을 scheduler pause 해제로 한정
- `/liquidate_all`을 OhMyStock 관리 포지션으로 한정하고 미관리 잔고 경고
- Bot API origin·TLS·redirect·proxy·로그 redaction 계약 추가
- durable inbox, 폭주 집계, 분할 delivery, 민감 payload 조기 폐기 추가
- inbox와 execution intent 상태 머신 및 scheduler gave_up 생산 경로 확정

두 차례 수정·재검토 후 네 리뷰어 모두 Critical/Important 없음으로
승인했다.
