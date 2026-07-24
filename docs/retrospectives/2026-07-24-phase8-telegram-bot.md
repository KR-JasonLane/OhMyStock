# Phase 8 텔레그램 봇 회고

## Task 1 — 순수 알림 모델·파서·인증·포맷

### 요청과 기존 상태

Phase 8의 첫 구현 단위로 Telegram·FastAPI·SQLAlchemy에 의존하지 않는 알림
도메인 계약을 추가했다. 기존에는 `app.domain.notifications` 패키지가 없어
명령 텍스트의 안전한 해석, 개인 운영자 인증, 발신 메시지 분할을 독립적으로
검증할 수 없었다.

### 설계 판단

- 명령 종류와 수신·운영자·운영 사건은 불변 dataclass 및 enum으로 표현해
  이후 adapter/store가 Telegram DTO나 ORM 객체를 domain에 노출하지 않게 했다.
  `OperationalEvent.payload`는 JSON 호환 검증 뒤 중첩 mapping/list까지 동결한
  스냅샷이고 사건 시각은 timezone-aware여야 한다. 저장소에는
  `payload_for_storage()`가 매번 새 JSON-호환 mutable 복사본을 제공하므로,
  저장소 측 변이가 도메인 사건을 바꾸지 않는다.
- `/confirm`만 16~64자의 영숫자·`_`·`-` 토큰을 인자로 허용한다. 파서는
  토큰 문법을 확인한 즉시 SHA-256 digest만 `argument_hash`로 반환한다. 따라서
  파싱 결과의 필드·표현에는 원문 토큰이 남지 않으며 저장·로그 기록은 후속
  durable inbox의 책임으로 남긴다. 명령 토큰은 정확한 `/name` 형식만 허용해
  다른 봇 suffix를 수용하지 않는다.
- 인증은 user ID, chat ID, private chat, forwarded 아님을 모두 요구한다.
  `InboundMessage` 생성자는 forwarded 여부를 반드시 받으므로 어댑터 누락은
  안전하지 않은 기본값 대신 즉시 실패한다. 값은 정확한 `bool`만 허용하며
  인증도 `forwarded is False`일 때만 통과한다.
  운영자 컬렉션을 받아 초기 단일 운영자 정책과 향후 복수 운영자 확장을 함께
  지원한다.
- 텍스트는 Telegram 마크업을 지정하지 않고 plain text로만 렌더링한다. 각
  조각에 상관 ID와 순번을 붙여 at-least-once 전송의 중복 식별과 재조립을
  가능하게 했다. 상관 ID·순번·총 조각 수를 포함한 실제 header 길이를 매번
  반영해 모든 조각이 caller의 길이 한도 이하다. 상관 ID는 1~64자의
  `[A-Za-z0-9_-]`만 허용하고, header를 담지 못하는 한도 및 잘못된 순번은
  명시적으로 거부한다. 개행 우선·핵심 거래 상태 반복은 이 일반 renderer가
  사건 의미를 알 수 없으므로 후속 projector의 책임으로 남긴다.

### 변경 위치

- `backend/app/domain/notifications/models.py`: 명령·우선순위 enum과 불변 값 객체
- `backend/app/domain/notifications/parsing.py`: slash 명령 및 확인 토큰 검증
- `backend/app/domain/notifications/authorization.py`: 운영자 개인 채팅 인증 판정
- `backend/app/domain/notifications/formatting.py`: plain-text 고정 chunk 렌더링
- `backend/app/domain/notifications/__init__.py`, `ports.py`: 패키지 및 후속 외부 경계 자리
- `backend/tests/notifications/test_parsing_authorization.py`: 명령·인증 계약
- `backend/tests/notifications/test_formatting.py`: 마크업 미사용·순번·상관 ID 계약

### 검증 결과

- RED: `uv run pytest tests/notifications/test_parsing_authorization.py -q`가
  `ModuleNotFoundError: app.domain.notifications`로 실패함을 확인했다.
- GREEN: 같은 테스트 7건 통과 후, formatting 모듈 부재 RED를 확인했다.
  경계 검토에서 고정 64자 예약만으로는 실제 header 길이를 보장하지 못함을
  발견해 길이·작은 한도·개행 상관 ID 회귀 테스트를 추가하고 수정했다.
  `uv run pytest tests/notifications -q`는 15건을 통과했다.
- 리뷰 수정 RED: bot suffix 수용, raw confirmation 보존, shallow payload,
  forwarded 기본값, 느슨한 correlation ID와 잘못된 조각 순번을 각각 재현한
  11개 실패를 확인했다. SHA-256 전용 결과, 깊은 JSON 동결, fail-closed
  생성자와 renderer 검증으로 수정 후 알림 도메인 26건이 통과했다.
- 2차 수정 RED: non-bool forwarded 수용, 동결 payload 재입력 실패, 저장용
  mutable JSON 복사본 부재와 JSON key/scalar 검증 공백을 재현했다. 수정 후
  알림 도메인 33건이 통과했다.
- 전체: `cd backend && uv run pytest` 결과 815 passed, 11 deselected,
  기존 StarletteDeprecationWarning 1건이다. 외부 Telegram·키움 호출은 없다.

## Task 2 — fail-fast 설정과 안전한 Bot API 어댑터

### 요청과 기존 상태

Telegram 설정 3종의 기동 게이트와 공식 Bot API `getUpdates`/`sendMessage`
경계를 추가했다. 기존 `Settings`에는 Telegram 설정이 없었고, Telegram DTO를
Task 1의 `InboundMessage`로 변환하거나 Bot API 실패를 안전하게 분류하는
어댑터도 없었다.

### 설계 판단

- bot token, 허용 user ID, 허용 chat ID는 전부 없으면 비활성이고 일부만
  있으면 `Settings` 생성 단계에서 실패한다. 두 ID는 양수만 허용한다.
  리플레이 환경은 세 값이 정상이어도 `telegram_enabled=False`로 유지한다.
  token은 `SecretStr`로 보관한다.
- origin은 `https://api.telegram.org` 상수로 고정했다. 생성 client는 TLS
  검증, redirect 미추적, proxy 환경변수 무시, 35초 timeout을 사용한다.
  `getUpdates`는 long poll 30초, `allowed_updates=["message"]`, limit 100을
  고정한다. 외부 네트워크 대신 주입 가능한 `AsyncBaseTransport`만 테스트에
  사용했다.
- update의 정수·문자열 필드는 정확한 타입으로 검증해 Task 1
  `InboundMessage`로 바꾼다. text가 없는 일반 message는 빈 문자열로
  정규화해 후속 파서가 거부할 수 있게 하고, `forward_origin` 또는 구형
  `forward_date` 존재 여부를 정확한 `bool`로 명시한다.
- `sendMessage`에는 `chat_id`와 `text`만 보내 `parse_mode`가 없는 plain
  text 계약을 지키며 정상 응답의 정확한 정수 `message_id`를 반환한다.
  401/403은 응답 본문 형식과 무관하게 client 공유 인증 회로를 열어 poller와
  sender 양쪽의 후속 HTTP 호출을 차단한다. 429는 `retry_after`를 보존하고,
  redirect·4xx·Telegram `ok=false`·JSON/형식 오류는 영구 오류, 5xx와
  네트워크 오류는 일시 오류로 분류한다.
- 예외는 endpoint label과 분류만 가진다. URL을 보존할 수 있는 httpx 예외
  cause를 끊는다. 중앙 logging 설정에서 httpx/httpcore access log를
  WARNING 미만으로 차단한다. client는 `aclose()`와 async context manager를
  모두 제공한다.

### 변경 위치

- `backend/app/core/config.py`: Telegram 설정, all-or-nothing/양수 ID 검증,
  replay-aware `telegram_enabled`
- `backend/app/adapters/telegram/client.py`: 고정 origin Bot API client,
  DTO 변환, 오류 분류·인증 회로·로그 비밀 제거, 명시적 수명주기
- `backend/app/adapters/telegram/__init__.py`: 공개 어댑터 계약
- `backend/tests/test_config.py`: 설정 기동 게이트 회귀
- `backend/tests/adapters/test_telegram_client.py`: 전송·정규화·오류·비밀 계약
- `backend/tests/conftest.py`: 일반 테스트의 Telegram 설정 제거
- `.env.example`: Telegram 3종 설정과 운영 주의사항

### 검증 결과

- RED: `uv run pytest tests/test_config.py -q`에서 신규 설정 테스트 12건이
  의도대로 실패했고, 어댑터 테스트 수집은
  `ModuleNotFoundError: app.adapters.telegram`으로 실패했다.
- GREEN: `uv run pytest tests/test_config.py
  tests/adapters/test_telegram_client.py -q` 결과 최초 38건, 리뷰 수정 후
  43건이 통과했다.
- 리뷰 패널은 message ID 유실, 비-message poison update의 batch 전파,
  비-JSON 401/403 회로 미작동, JSON 파싱 cause의 불신 본문 보존,
  테스트의 dotenv 상속 가능성을 Important로 발견했다. message ID를
  검증·반환하고, 유효 update ID를 가진 미지원 항목은 인증 불가능한 최소
  메시지로 격리해 offset을 보존했다. HTTP status를 JSON보다 먼저 판정하고
  외부 cause를 끊었으며 테스트에서는 dotenv source 자체를 차단했다.
- 수정 후 senior-developer, senior-trader, architecture-expert,
  security-expert 네 관점의 재검토에서 Critical/Important 0으로 전원
  승인받았다. 이 변경은 키움 API 경로에 닿지 않아 broker-api-expert는
  적용하지 않았다.
- 공식 최종 패널의 추가 Important에 따라 오류 decision table을 body와
  분리했다. HTTP 401/403은 인증 회로, 429는 기본 1초를 포함한 rate limit,
  408은 timeout, 5xx는 server 일시 오류이며 3xx와 나머지 4xx는 영구
  오류다. 2xx Telegram envelope의 exact-int `error_code`도 같은 의미로
  분류하고 bool/string/float는 불신 형식 오류로 거부한다.
- polling batch는 객체가 아니거나 exact-int `update_id`가 없는 항목만
  개별 skip한다. 유효 ID의 미지원 update는 기존 `unsupported` sentinel로
  반환한다. 따라서 손상 항목 뒤의 `/stop`도 소비자가 받아 그 ID까지
  offset을 전진할 수 있다. 더 큰 malformed ID 자체는 값을 신뢰할 수 없어
  offset 근거로 쓰지 않는다. 그 항목이 다시 보여도 이후 정상 update는
  독립 정규화되므로 poison 항목이 정상 명령을 차단하지 않는다.
- client별 전역 log filter를 제거했다. `app.core.sensitive_logging`이
  `httpx`와 `httpcore` namespace를 WARNING으로 두어 child access trace도
  상속 차단하며 warning/error 관측성은 유지한다. Telegram client 모듈
  import와 앱 중앙 logging 초기화에서 idempotent하게 적용한다.
- 공식 패널 수정 후 최종 focused 범위(`tests/test_config.py`,
  `tests/adapters/test_telegram_client.py`, `tests/test_app_lifespan.py`)는
  64건이 통과했다.
- 전체: `cd backend && uv run pytest` 결과 861 passed, 11 deselected,
  기존 StarletteDeprecationWarning 1건이다. `uv run python -m compileall
  -q app tests`도 exit 0이다.
- 실제 Telegram 호출은 수행하지 않았으며 모든 어댑터 요청은
  `httpx.MockTransport`로만 검증했다.

## Task 3 — 0013 영속 모델과 저장소

- 요청: polling offset과 update batch의 원자 커밋, 확인 토큰의 단일 소비,
  command intent, append-only operational event, 고정 조각 delivery를
  재기동 가능한 SQL 상태로 만들었다.
- 기존 상태: `0012`가 Alembic head였고 Telegram용 테이블과 저장소는 없었다.
- 설계 판단: claim은 후보 조회만으로 소유권을 인정하지 않고 상태·version·
  lease를 다시 조건으로 건 UPDATE의 rowcount가 1일 때만 성공으로 본다.
  terminal 상태는 claim 조건에서 제외했다. confirmation은
  `secrets.token_urlsafe(32)` 원문을 호출자에게 한 번만 반환하고 SHA-256
  해시만 저장하며 TTL은 호출 인자와 무관하게 120초로 고정했다.
  JSON은 key 정렬·공백 제거·NaN 금지 canonical 형식으로 저장한다.
- 변경 위치: `backend/alembic/versions/0013_telegram_notifications.py`,
  `backend/app/store/models.py`, `telegram_inbox_store.py`,
  `telegram_command_store.py`, `notification_store.py`와 저장소/마이그레이션
  테스트. SQLite의 자동 증가 의미를 보존하기 위해 PK 타입만 dialect
  variant를 사용하며 PostgreSQL에서는 BigInteger다.
- 안전성: batch와 offset, token 소비와 intent 생성은 각각 하나의
  `sessions.begin()`에 있다. delivery retry 예산과 lease는 조각별이며 모두
  전송되면 payload와 모든 body를 NULL로 purge한다. retention은 sent
  notification만 제한 건수로 지우며 pending/sending/dead-letter,
  command unknown과 audit 근거에는 손대지 않는다. naive datetime은 store
  경계에서 즉시 거부한다.
- 검증: focused 저장소 테스트와 0013 upgrade/downgrade 왕복을 실행했다.
  운영 DB upgrade와 Telegram/키움 네트워크 호출은 하지 않았다.

### Task 3 공식 패널 수정

- command 실행 상태를 `pending → claimed → running`으로 제한했다. 만료된
  `running`은 재실행하지 않고 CAS로 `unknown` 표시한 뒤
  `unknown → reconciling` 전용 claim에서만 대사한다. 모든 실행·종결 전이는
  owner와 version fence가 일치해야 한다.
- synthetic 음수 update를 제거했다. confirmation 소비는 기존 inbox의
  실제 `/confirm` update ID가 필수이며, token 소비와 intent insert는 같은
  트랜잭션이다. 운영자별 lock row를 upsert하고 `FOR UPDATE`로 발급을
  직렬화한다.
- delivery에 version fence를 추가하고 success/retry 우회 API를 제거했다.
  마지막 조각은 outbox를 `FOR UPDATE`로 잠근 뒤 전체 성공을 확인하고
  payload/body를 purge한다. claim 순서는 priority, occurred_at,
  part_index다.
- update/event/outbox insert는 SQLite/PostgreSQL dialect upsert 또는
  unique conflict 흡수 구조로 바꾸고 offset 갱신은 DB 조건부 단조 증가로
  만들었다. operational event는 도메인 모델만 받으며 caller session에
  참여하는 primitive를 제공한다.
- 저장 경계는 hash·command·식별자·canonical JSON·본문 byte 상한을
  fail-fast 검증한다. 미허용 폭주는 Counter로 메모리 집계하고 분당
  주체 행과 고정 집계 bucket을 합쳐 최대 300행으로 제한한다.
- 민감 조회/digest에는 각각 15분/24시간 purge 시각을 저장한다. 기한이
  지나면 미전송이어도 본문을 지우고 dead-letter 메타데이터는 남긴다.
  terminal update·confirmation·sent outbox 정리는 limit가 있는 API만
  제공하며 unknown, pending/sending, 조사 전 dead-letter와 연결된 감사
  근거는 삭제하지 않는다.
- SQLite는 `FOR UPDATE`를 실제 row lock으로 집행하지 않는다. 테스트는
  conditional UPDATE/unique constraint의 단일 승자 의미를 검증하고,
  프로덕션 PostgreSQL에서는 코드에 보존한 `FOR UPDATE`가 발급과 마지막
  조각 종결을 직렬화한다.

### Task 3 2차 재검토

- `reconciling` worker도 죽을 수 있으므로 lease가 만료된 reconciliation을
  owner+version CAS로 다른 worker가 회수하게 했다. stale worker의 terminal
  기록은 fence에서 거부된다.
- 모든 notification 변경은 parent outbox lock을 delivery보다 먼저
  획득한다. TTL purge는 살아 있는 sending lease가 있으면 건너뛰고 만료 후
  outbox lock→delivery version 증가·본문 폐기→outbox payload 폐기 순서다.
  claim은 이미 purge 시각이 지난 본문을 가져오지 않는다.
- confirmation 90일 정리는 unresolved execution 연결만 보존한다. terminal
  execution은 제한된 후보 범위에서 nullable FK를 해제한 뒤 confirmation을
  지운다.
- rejected iterable은 최대 100개까지만 소비한다. 분당 total 300 이후에는
  외부 SHA-256 값과 문법상 충돌할 수 없는 `__total__`, `__overflow__` 내부
  bucket 두 행만 갱신해 write amplification을 막는다. retention도 limit와
  인덱스를 갖는다.
- 메시지는 조각당 4,096 **문자**, 최대 64조각, 전체 UTF-8 256 KiB로
  제한한다. update/offset은 bool·문자열을 포함한 비 exact-int를 거부하고,
  correlation/owner는 `[A-Za-z0-9_-]{1,64}`로 통일했다.
- timezone-aware 검증은 `telegram_common.py`로 이동해 세 저장소가 같은
  fail-fast 계약을 사용한다.

## Task 4 — REST·Telegram 공용 OperationsControl과 계좌 snapshot

### 요청과 기존 상태

REST 라우터 안에 있던 scheduler 제어와 trading 정지 진입점을 Telegram도
HTTP 자기 호출 없이 재사용할 수 있는 공용 유스케이스로 분리했다. 기존에는
계좌 예수금과 잔고를 하나의 기준 시각으로 조회하거나, 당일 실현손익의
KST 귀속일·정확도·부분 실패를 함께 표현하는 계약이 없었다.

### 설계 판단

- `OperationsControl`은 FastAPI와 HTTP 예외를 알지 않는다. scheduler 상태
  지문, pause/resume, 계좌·포지션 snapshot, 멱등 stop, 확인된 관리 포지션
  청산만 조정한다. `/resume`은 scheduler의 인메모리 pause만 해제하며
  trading kill-switch에는 접근하지 않는다.
- 예수금과 잔고는 `asyncio.gather`로 병렬 조회한다. 같은 명령의 진행 중
  task와 10초 캐시를 공유하며, digest는 5초 timeout을 적용한다. 일부 소스
  실패는 `failed_fields`에 남기고 성공한 필드를 폐기하지 않는다. 총
  수익률은 검증된 분모가 없으므로 항상 `None`이다. 실현손익은 현재 run
  environment와 KST 거래일로 집계하며, broker 체결 대사 표식이 아직 없어
  `estimated`로 반환한다.
- `LiquidationTarget`과 `LiquidationResult`는 trading domain이 소유한다.
  확인 시점의 `(position_id, symbol, quantity)`와 실행 직전 DB snapshot이
  다르면 발주하지 않고 `needs_attention`을 반환한다. 실행 수명 동안
  `intent_id`를 보조 멱등 가드로 쓰며, durable SSOT는 Task 5 command
  execution이다. broker 잔고·미체결 조회 실패나 잔량·미체결·미종결 DB
  상태는 종목과 수동 조치를 포함한 `needs_attention`으로 보존한다.
- target-confirmed 청산에서는 `_load_entered()`를 target ID로 제한한다.
  미관리 broker 잔고는 청산 대상에 넣지 않고 `account_fully_empty=False`
  및 경고로 노출한다. 기존 REST `mode=liquidate_all`은 현재 run
  environment의 엔진 관리 포지션 전부를 대상으로 하는 기존 계약을
  유지한다. Telegram 확인 경로만 고정 target snapshot을 사용한다.

### 변경 위치

- `backend/app/core/operations_control.py`: 공용 상태·계좌·제어 유스케이스
- `backend/app/domain/trading/models.py`: 청산 target/result 값 객체
- `backend/app/domain/trading/service.py`: intent 보조 가드, target 검증·대사
- `backend/app/store/trading_store.py`: ID/환경 포지션 조회, KST 당일 실현손익
- `backend/app/api/schedule.py`, `backend/app/api/trade.py`: 공용 제어 호출과
  기존 REST 응답·상태 계약 보존
- `backend/tests/test_operations_control.py`: resume, 병렬 계좌 부분 성공,
  관리·미관리 preview 계약

### 검증 결과

- RED: `uv run pytest tests/test_operations_control.py -q`가
  `ModuleNotFoundError: app.core.operations_control`로 실패함을 확인했다.
- focused: operations/schedule/trade/trading service 45건 통과.
- 전체: `uv run pytest -q` 결과 888 passed, 11 deselected, 기존
  StarletteDeprecationWarning 1건이다.
- 실제 broker·키움 API와 운영 DB는 호출하지 않았다.

### 공식 패널 수정

- 빈 target은 성공 no-op으로 종결해 broad `LIQUIDATE_ALL`로 승격되지 않게
  했다. 일반 `_load_entered()`의 managed 필터를 제거하고 명령 전용
  `_load_managed_entered()`로 격리했다. 새 run 수락 시 이전 scope를 지워
  이후 정상 감시와 기존 REST broad 청산에 영향이 남지 않는다.
- managed 청산은 실행 중 run, 거래일·장중, 종목 거래정지, 확인 target의
  ID·symbol·quantity, broker 잔고 수량을 발주 전에 검사한다. DB와 broker
  수량은 symbol별로 합산해 정확히 같아야 하며, 동일 symbol 수동 잔고가
  섞였다고 의심되면 보수적으로 거부한다. 기존 SELL 미체결도 symbol별
  잔량을 합산하고 하나라도 있으면 `in_progress`로 반환해 중복 매도하지
  않는다. 주문 직전 같은 preflight와 전용 loader가 TOCTOU 수량 변화를
  다시 차단한다.
- managed 요청은 협조적 stop 직후 terminal 실패로 오판하지 않고
  `accepted`를 반환한다. idle, 장외, 거래정지, 대사 실패는 주문 0건과
  구체적 `needs_attention`이다. 동시에 다른 intent가 active scope를
  덮어쓰지 못한다.
- intent ID는 1~64자 안전 문법과 256개 상한을 적용했다. async lock 아래
  mode 충돌을 fail-loud로 판정하고 side effect 성공 뒤에만 applied map에
  남긴다. 예외·취소 전 applied로 오기록하지 않아 같은 intent 재시도가
  가능하다. REST 임시 intent도 객체 메모리 주소 대신 난수 128비트를 쓴다.
- lifespan이 `OperationsControl`을 한 번만 만들고 REST와 후속 Telegram이
  같은 cache/single-flight를 공유한다. 라우터 요청별 fallback은 제거했고
  조립 누락은 503이다. digest는 fresh cache 또는 이미 실행 중인 interactive
  조회만 공유하며 스스로 새 broker 호출을 시작하지 않는다.
- `/trade/status`와 `/trade/positions` 무인증 계약은 Phase 7 배포 게이트의
  기존 비범위 결정이다. Task 4는 응답 필드·인증 범위를 바꾸거나 계좌
  snapshot을 이 두 endpoint에 추가하지 않았다. 기존 API 계약 테스트를
  그대로 통과시켜 노출 확대가 없음을 확인했다.
- 공식 패널 수정 후 focused 53건, 전체 896건 통과, 11건 live deselect와
  기존 Starlette deprecation warning 1건이다.

### 공식 패널 재검토 수정

- full managed preflight는 자체 첫 SELL 전에 한 번만 수행한다. 청산 loop는
  이후 full preflight를 반복하지 않고 monitor가 소유한 pending을
  `CLOSED`/`EXIT_FAILED`까지 poll한다. 따라서 첫 자체 SELL이 다음 loop의
  “기존 SELL” 검사에 걸려 감시가 조기 종료되지 않으며, 부분 terminal target이
  생겨도 남은 target/pending을 계속 처리한다.
- 최초 기존 SELL은 target symbol만 검사한다. 비대상 symbol의 SELL은 이
  명령을 막지 않는다. target SELL은 이 프로세스가 소유한 주문이라는 durable
  근거가 없으므로 `in_progress`로 가장하지 않고 주문번호와 symbol별 합산
  잔량을 포함한 terminal `needs_attention`으로 반환하며 신규 주문은 0건이다.
- `request_stop_durable()`은 `StopRequestResult(applied, persisted,
  warning)`을 반환한다. DB 영속 실패를 삼키더라도 인메모리 stop이 적용됐고
  durable 감사는 실패했다는 두 사실을 분리한다. 기존 호출자는 반환값을
  무시할 수 있어 호환되며, 공용 제어/후속 worker는 `persisted=False`를
  unknown/needs_attention 근거로 쓸 수 있다. managed 경로는 persistence
  실패 시 scope/intent를 commit하지 않고 같은 event-loop turn에서
  `STOP_NEW_ENTRIES`로 downgrade해 broad 청산 유출을 막는다.
- active managed scope는 stop 영속 성공 뒤에만 commit한다. 완료 결과는
  intent별 bounded(256) 메모리 map에 보존해 새 run이 active scope를
  clear한 뒤에도 `reconcile_control_intent()`가 Task 5 worker에 terminal
  결과를 넘길 수 있다. 프로세스 재기동 뒤에는 Task 5의 durable target과
  broker 잔고·미체결·DB 상태로 재대사해야 하며, 이 태스크는 DB command
  execution write를 추가하지 않는다.
- managed 통합 테스트는 첫 SELL 1건, 다음 loop의 미체결 반복, 신규 SELL
  없음, 후속 CLOSED terminal을 검증한다. 별도 장마감 시나리오는 broker
  잔량을 `EXIT_FAILED`로 보정하고 terminal `needs_attention`을 보존한다.
  새 run이 managed scope만 clear한 뒤 기존 REST broad loader가 두 관리
  포지션 모두를 반환하는 회귀도 고정했다.
- 재검토 수정 후 focused 58건, 전체 901건 통과, 11건 live deselect와 기존
  warning 1건이다.

### 마지막 Important 수정

- 같은 managed intent의 terminal 결과가 bounded cache에 있으면 active
  scope 판정보다 먼저 반환한다. 따라서 Task 5 worker와
  `OperationsControl.liquidate_managed()`의 재조회가 다음 run에서도
  `unknown`/`in_progress`로 퇴행하지 않고 기존 terminal을 받는다.
- 일반 `request_stop_durable()`은 기존 계약대로 DB await 전에 인메모리
  stop을 즉시 적용한다. 느린 store barrier 테스트에서 persistence task가
  대기 중이어도 `STOP_NEW_ENTRIES`가 보임을 확인했다.
- managed는 `_persist_stop_request()`로 DB만 먼저 영속한다. 성공 뒤 같은
  event-loop turn에서 scope/targets를 설치하고 마지막에
  `request_stop(LIQUIDATE_ALL)`을 호출한다. 따라서 느린 DB 동안 scope 없는
  broad flag가 없고, 영속 실패·await 취소 시 scope와 mode가 모두 비어 있다.
  다만 `asyncio.to_thread` 취소는 이미 시작된 DB thread 자체를 중단하지
  못하므로 DB에는 의도가 기록됐지만 caller는 결과를 못 받은 ambiguity가
  가능하다. 이 경우 Task 5 durable command target과 broker 주문·잔고·DB
  상태를 대사한 뒤에만 재실행한다.
- 마지막 수정 후 focused 62건, 전체 905건 통과, 11건 live deselect와 기존
  warning 1건이다.

### 취소·run 종료 경합 최종 수정

- managed persistence를 별도 asyncio task로 만들고 shield한다. caller가
  취소돼도 이미 시작된 `to_thread`의 성공/실패 결과를 lock 안에서 끝까지
  회수한다. 영속 성공이고 run이 살아 있으면 먼저 scope/targets를 게시하고
  마지막에 `LIQUIDATE_ALL` flag를 await 없이 설정한 뒤 cancellation을 다시
  전파한다. 영속 실패면 scope/mode 없이 취소를 전파한다.
- persistence barrier 동안 run이 자연 종료될 수 있으므로 영속 성공 직후
  `is_running()`을 다시 확인한다. 종료됐다면 active scope와 liquidation
  flag를 게시하지 않고 `run ended before liquidation started` terminal
  `needs_attention`을 bounded 결과에 저장한다. 정상 반환과 caller 취소
  양쪽에서 이후 동일 intent 조회가 이 terminal을 받는다.
- barrier 테스트는 DB 행의 `kill_switch_mode`까지 확인한다. 취소+DB 성공+
  run 생존은 DB와 active scope/mode가 함께 존재하고, run 종료는 DB 의도만
  남되 active scope/mode 없이 terminal cache가 존재한다. 이는 확인과
  실행 사이 run 수명 경합을 성공으로 가장하지 않는 계약이다.
- 최종 focused 64건, 전체 907건 통과, 11건 live deselect와 기존 warning
  1건이다.

### ABA·반복 취소 최종 수정

- managed lock 진입 때 `expected_run_id`를 캡처한다. preflight 뒤와 DB 결과
  회수 뒤 모두 `is_running()` 및 현재 run ID 동일성을 검사한다. persistence
  helper도 현재 필드를 다시 읽지 않고 캡처한 ID를 명시적으로 받는다.
  A persistence 대기 중 A가 끝나고 B가 시작되면 DB 의도는 A에만 기록되고,
  B에는 scope·flag를 게시하지 않으며 terminal `run changed/ended`
  needs_attention을 cache한다.
- shielded persistence를 기다리는 동안 두 번째 이상의 cancellation이 와도
  `Task.uncancel()`로 각 요청을 명시적으로 소진하고 shield loop를 반복한다.
  DB task가 terminal이 된 뒤 성공/실패 및 run identity에 맞게 메모리 상태를
  확정하고, cancellation 발생 사실이 있으면 마지막에 한 번 다시 전파한다.
- double-cancel 성공 테스트는 A의 DB mode와 active managed scope/mode가 함께
  게시됐음을, 실패 테스트는 DB mode·scope·mode가 모두 없음을 확인한다.
  ABA barrier 테스트는 A 행만 `liquidate_all`, B 행은 NULL임을 직접 검증한다.
- 최종 focused 66건, 전체 909건 통과, 11건 live deselect와 기존 warning
  1건이다.
