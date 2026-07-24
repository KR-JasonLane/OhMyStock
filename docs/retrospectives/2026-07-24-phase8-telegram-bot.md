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

## Task 5 — durable 수신·확인·제어 command worker

### 요청과 기존 상태

Task 3은 inbox, confirmation, command intent의 SQL 상태 기계만 제공했고,
Task 4는 REST와 Telegram이 공유할 OperationsControl 및 managed liquidation을
제공했다. 그러나 inbox claim을 intent의 선행 영속, 공용 제어 호출, audit,
재기동 대사로 연결하는 worker는 없었다. 특히 Task 4의 TradingService에는
`reconcile_control_intent()`가 있었지만 OperationsControl에 같은 공용 경계가
없어 Telegram 도메인이 구현체 내부로 내려갈 위험이 있었다.

### 설계 판단

- `CommandProcessor`는 raw Telegram text나 broker/order port를 받지 않고
  정규화된 inbox 행, `TelegramCommandStore`, `OperationsControlPort`만 소비한다.
  동기 SQLAlchemy 저장소 호출은 모두 `asyncio.to_thread()`로 격리했다.
- `/pause`, `/stop`과 조회 명령은 update ID 기반 execution intent를 먼저
  만들고, 해당 ID만 claim하여 `running` 전이 뒤 공용 제어를 호출한다. 다른
  pending intent를 우연히 claim하는 것을 막아 update 순서와 부수효과 귀속을
  보존한다. `/account`, `/positions` 결과에는 이후 outbox가 영속 `sensitive`
  값을 쓰도록 `outbox_sensitive=True`를 명시한다.
- `/resume`, `/liquidate_all` 첫 요청은 intent나 청산을 만들지 않고 120초
  confirmation만 발급한다. `/confirm`은 hash·operator·설정 chat hash에
  귀속된 미사용 confirmation을 먼저 조회하고, 현재 scheduler/관리 target
  fingerprint가 같은 경우에만 token 소비와 intent 생성을 원자 처리한다.
  청산 target `(position_id, symbol, quantity)`는 intent JSON snapshot으로
  보존하고 다시 `LiquidationTarget`으로 복원한다.
- control 호출 직후의 프로세스 단절이나 예외는 `running → unknown`으로만
  남긴다. 재기동 `reconcile_unknown()`은 liquidation에 대해
  `OperationsControl.reconcile_control_intent()`만 호출하며 새 liquidation이나
  broker SELL을 직접 실행하지 않는다. terminal cache/기존 주문·잔고·DB
  대사는 Task 4 TradingService 소유로 유지한다. 비청산 unknown은 실행 여부를
  추측하지 않고 `needs_attention`으로 종결한다.
- Task 4 경계의 누락은 최소 보완했다. `OperationsControl`은 trading 미조립
  시 구조화된 `needs_attention`을 반환하고, 그 외에는 TradingService의
  reconcile만 위임한다. notifications `OperationsControlPort` protocol에도 이
  계약을 명시해 도메인이 broker 구현에 의존하지 않게 했다.

### 변경 위치

- `backend/app/domain/notifications/commands.py`: inbox lease, immediate/risky
  command 분기, confirmation fingerprint, intent 실행·unknown reconciliation
- `backend/app/domain/notifications/ports.py`: 공용 OperationsControl protocol
- `backend/app/store/telegram_command_store.py`: update 기반 intent 생성,
  특정 intent claim, confirmation context read, command audit/status 조회
- `backend/app/core/operations_control.py`: durable liquidation reconcile 위임
- `backend/tests/notifications/test_commands.py`: intent 선행, 위험 명령,
  confirm, 재기동 재매도 방지, 전체 command/sensitive 결과 회귀
- `backend/tests/store/test_telegram_stores.py`: update당 단 하나의 즉시 intent
- `backend/tests/test_operations_control.py`: 공용 reconcile 위임 계약

### 검증 결과

- RED: `cd backend && uv run pytest tests/notifications/test_commands.py -q`가
  `ModuleNotFoundError: app.domain.notifications.commands`로 실패함을 먼저
  확인했다. OperationsControl reconcile wrapper 추가 전에는 같은 단위 테스트가
  `AttributeError`로 실패함도 확인했다.
- GREEN/focused: `uv run python -m compileall -q app tests`와
  `uv run pytest tests/notifications/test_commands.py
  tests/store/test_telegram_stores.py tests/test_operations_control.py -q`가
  43건 통과했다.
- 전체: `uv run pytest -q` 결과 923 passed, 11 deselected, 기존
  `StarletteDeprecationWarning` 1건이다.
- 실제 Telegram/키움 API 및 운영 DB는 호출하지 않았다.

### 패널 수정·재검토 전 통합 보완

- command intent ID를 `telegram_command_update_<update_id>`와
  `telegram_command_confirmation_<confirmation_id>`로 통일했다. Task 4
  TradingService가 공용 제어 ID에 허용하는 `[A-Za-z0-9_-]{1,64}`와 맞추므로
  `/stop`과 확정 청산이 validation 오류로 unknown에 남지 않는다.
- control 호출이 30초보다 길어져도 `running` lease가 살아 있도록 worker가
  10초 heartbeat CAS 갱신을 수행한다. 갱신 fence를 잃으면 terminal 성공을
  기록하지 않고 durable recovery로 넘긴다. 느린 pause 동안 시계를 lease보다
  넘긴 뒤 concurrent reconciliation을 호출하는 회귀가 terminal 전이를 막음을
  검증했다.
- 프로세스 재기동에서는 in-memory managed scope가 없으므로,
  `reconcile_control_intent(intent_id, targets)`가 durable target snapshot을
  TradingService에 전달하도록 OperationsControl 경계를 확장했다. 서비스는
  broker 잔고·기존 미체결 SELL·DB position 상태만 읽어 `succeeded` 또는
  `needs_attention`을 판정하고 신규 주문을 절대 내지 않는다. fresh service
  회귀는 broker 잔고 0/미체결 없음/DB CLOSED에서 성공과 SELL 0건을 고정했다.
- `/stop`의 control 반환 `False`는 성공으로 감사하지 않고 `needs_attention`으로
  종결한다. 비청산 unknown은 현재 scheduler paused 및 trading kill-switch
  snapshot만 읽어 pause/resume/stop의 성공 여부를 판정하고, 원래 부수효과를
  재호출하지 않는다. reconciliation audit도 sentinel `0` 대신 실제 원본
  update ID를 기록한다.
- confirmation fingerprint의 run ID는 해시 검증에만 쓰지 않고 intent target
  context에도 보존한다. `OperationsControl → TradingService`는 이
  `expected_run_id`를 lock 안에서 현재 run과 다시 비교하므로 confirmation 뒤
  run 교체가 있으면 신규 청산을 시작하지 않는다.
- managed liquidation이 `accepted`를 반환하면 command worker는 별도 감독 task로
  running lease를 갱신하고 Task 4의 read-only reconciliation이 terminal을 줄
  때만 종결한다. active managed scope의 잔량/미체결은 `in_progress`로 남겨
  정상 SELL 폴링을 조기 `needs_attention`으로 오기록하지 않는다. heartbeat
  종료는 in-flight DB renewal까지 join한 뒤 version fence를 사용한다.
- `process_claimed()` 공개 진입점을 추가해 Task 7 TelegramService가 inbox claim
  뒤 query/control lane으로 분배할 수 있게 했다. 간단한 `process_next()`는
  단일 worker 편의 API로 유지한다.

## Task 6 — append-only operational events와 체결 정확도

### 요청과 기존 상태

상태 테이블 사후 스캔 대신 append-only 사건을 Telegram outbox 원천으로 쓰고,
스케줄러 포기·부분체결·청산 잔량·킬스위치를 재기동 뒤에도 투영해야 했다. 0013에는
`operational_events`, outbox, 범용 `telegram_state`가 이미 있었지만 projector와
실제 trading/scheduler producer는 없었다.

### 설계 판단과 변경 위치

- `notification_store.py`가 cursor/outbox의 SQLAlchemy transaction을 소유하고,
  `domain/notifications/projector.py`는 순수 event→outbox 정책과 port만 안다.
  `telegram_state.event_projector_cursor`와 `operational:{id}:{kind}` 고유키로
  rewind 중복을 차단했다.
- `scheduler_store.py`는 GAVE_UP 원천 행 flush 뒤 같은 transaction에서
  `pipeline_gave_up`을 append하며, trade job에는 monitoring-gap 종류도 넣는다.
- `entry.py`와 `monitor.py`가 실제 관측한 partial fill을 callback으로 내보내고,
  `service.py`는 주문 감사 때 보존한 order ID로 `trading_store.py`에 귀속한다.
  모든 부분체결 payload는 original/fill/cumulative/remaining quantity,
  remaining order state, estimated 가격을 보존한다.
- 마지막 원자성 결함은
  `TradingStore.save_position_snapshot_with_fill_event()`로 해소했다. 같은
  SQLAlchemy session transaction에서 position/order 귀속을 검증하고 전체
  position snapshot과 partial-fill event를 함께 기록한다. append를 강제
  실패시킨 회귀에서 기존 snapshot과 event 0건이 함께 보존되어 rollback을
  확인했다.
- EntryExecutor와 PositionMonitor는 진입 지정가·진입 시장가·청산 시장가·
  익절 지정가·익절 시장가 fallback에서 관측 당시의 `TradePosition`
  snapshot을 fill facts와 함께 넘긴다. TradingService의 공통 callback만
  원자 API를 호출한다. 저장 실패 시 원자 transaction은 rollback하되 실제
  주문의 pending 추적과 진입 잔고 대사는 중단하지 않는다. append 장애가
  지속되는 비상 경로는 state-only snapshot과 `[audit-gap]` run warning을
  같은 별도 transaction에 남겨 감시 상태와 검색 가능한 감사 공백을 함께
  보존한다. 재기동 감시는 기존 order number를 감사 order ID로 재수화한다.
- pending 주문은 ka10075의 후속 미체결 수량 감소를 버리지 않고 증분·누적·
  잔량 사건을 갱신한다. 장마감 `exit_remaining_failed`도 마지막 누적을
  역행시키지 않는다. 재기동 때 원주문량·현재 미체결량·DB의 마지막 성공
  잔량과 append-only order event의 마지막 성공 누적을 복원해 callback 실패
  창의 delta도 재생한다. state-only audit-gap snapshot이 앞서가도 event
  기준선은 오염되지 않는다. 주문 소멸은 체결 확정
  근거가 아니므로 모든 청산 `CLOSED`가 즉시 kt00018 대사를 요구하고,
  terminal 사건은 실제 order ID에 마지막 누적/잔량을 보존한다. 부분체결
  가격이 없는 경우 잔량만으로 손익을 과소 계산하지 않고 미확정 `None`으로
  둔다. 주문 소멸 직후에는 `EXITING + exit_unconfirmed`만 원자 저장하고,
  kt00018 잔고 0을 성공 확인한 뒤에만 state-only CLOSED를 확정한다. 잔고
  조회 실패 시 EXITING이 재기동 스캔에 남는다.
- 전량 진입 사건은 kt00018 검증 전에는 만들지 않는다. 두 번의 잔고 확인
  뒤 실제 수량·평단 snapshot과 `entry_filled`/보정된 partial 사건을 함께
  기록하며, phantom이면 완료 사건을 남기지 않는다. kt00018 종목 합계는
  단일 주문 fill로 귀속하지 않고 polling 관측 수량을 event에 사용한다.
  시작 대사에서 DB 밖 기존 보유가 있는 심볼은 신규 진입을 fail-closed하고,
  실제 BUY 직전 잔고를 다시 확인해 startup 이후 수동/지연 체결 경합도 막는다.
  체결 후 계좌 종목 합계가 주문 관측 수량보다 크면 초과분은 자동매매 소유로
  흡수하지 않고 관측 수량만 저장하며 수동 검토 경고를 남긴다.
  unmanaged baseline은 entry operational event에 영속한다. 혼합 소유가
  확인되면 진입 사건을 원자 기록한 직후 포지션을 EXITING/manual로 격리해
  자동 매도를 막는다. 재기동·미니 reconcile·취소 후 잔고 정렬·청산 주문
  소멸 대사는 모두 같은 소유권 규칙을 쓴다. kt00018 종목 합계의 양수 수량은
  과거 baseline과 같아도 외부 거래와 전략 체결의 상쇄를 배제할 수 없으므로
  자동 CLOSED·수량 정렬·재오픈하지 않는다. 실제 합계 0만 CLOSED로 확정하고,
  그 외에는 EXITING과 kill-switch attention을 유지해 수동 대사로 보낸다.
  post-entry 검증 전에 죽은 PENDING_ENTRY도 주문별 fill 근거가 없으므로 양수
  합계를 ENTERED로 승격하지 않는다. ka10075 최초 주문 결측은 잔고 양수
  여부와 무관하게 유예 뒤 ka10075 주문을 먼저 재조회하고 그 뒤 kt00018을
  순서대로 확인한다. 주문이 계속 없고 post-absence 잔고도 0이면 다시 유예한
  최종 잔고까지 0일 때만 terminal 처리한다. DB 감사에서 주문번호를 복원하지
  못했는데 같은 심볼의 미귀속 live BUY가 있으면 그 주문을 전략 주문으로
  승격·취소하지 않고 포지션을 EXITING/manual로 격리한다. 격리 뒤 재기동에도
  BUY를 SELL 감시 주문으로 오인하지 않도록 PENDING은 귀속 BUY, EXITING은
  귀속 SELL만 주문번호와 방향을 함께 연결한다. 미귀속 BUY와 귀속 BUY가
  동시에 있으면 미귀속 주문은 건드리지 않고 귀속 BUY만 취소한다. ownership
  격리는 `EXITING + exit_reason 없음`을 durable discriminator로 사용해 live
  BUY가 소멸하고 잔고가 일시 0이어도 자동 CLOSED하지 않으며 명시적 수동
  해제까지 유지한다. 격리 중 귀속 SELL은 취소·재평가하지 않고 기존 주문만
  추적하며, pending과 ExitAction에도 구조화된 quarantine 표식을 운반해 SELL
  소멸·잔고 0 뒤에도 exit_reason과 EXITING을 보존한다. 이 표식은 시장
  마감의 `EXIT_FAILED` 전이에도 유지되며, 다음 reconcile은 잔고와 무관하게
  수동 `EXITING` 격리로 복구하고 자동 `CLOSED`하지 않는다. 창밖 생존 주문은
  취소 전 잔고로 ENTRY_FAILED를 확정하지 않고,
  취소 성공 뒤 대상 심볼만 fresh balance를 두 번 확인해 0일 때만 실패로
  종결하며 양수면 계획 수량과 같아도 EXITING 격리를 적용한다. 이 후속
  CLOSED/EXITING DB write도
  cancellation 중 실제 worker가 terminal이 된 뒤에만 취소를 재전파한다.
  ownership 격리 snapshot 저장이나 귀속 BUY 취소가 실패하면 marker와
  attention을 보존한 뒤 거래 run 자체를 fail-closed해 미확정 주문을 둔 채
  정상 루프로 진행하지 않는다.
- fill callback은 awaitable로 바꾸고 서비스의 원자 저장과 audit-gap fallback
  SQL을 모두 `asyncio.to_thread`에서 실행한다. PostgreSQL 지연이 FastAPI,
  scheduler, Telegram과 공유하는 event loop를 막지 않는다. persistence
  전체는 취소 안전한 소유 task로 감싸 worker와 fallback terminal 뒤에만
  취소를 재전파한다. 원자 저장 3회가 모두 실패하면 monitor에도 실패 신호를
  돌려 같은 process의 다음 poll에서 마지막 성공 누적부터 다시 시도한다.
  source order ID가 없는 진입 audit-gap도 동일하게 terminal까지 회수하며,
  terminal 예외/cancellation 경합은 결과 회수 뒤 타입만 로그로 남긴다.
- ka10075 주문 소멸은 체결가·fill ID를 주지 않으므로 `exit_unconfirmed`에
  전량 수량이나 가격을 꾸며 넣지 않았다. kt00018은 계좌/종목 단위 집계라
  동일 종목 복수 주문 중 특정 주문으로 안전하게 귀속할 실측 근거가 없어
  `broker_reconciled` 승격도 하지 않았다.

### 검증과 패널

- RED는 projector 모듈 부재, 진입 snapshot 사건 부재, 원자 API 부재로
  확인했다.
- Task 6 focused 187건, 백엔드 전체 996건이 통과했고 11건은 명시적으로
  제외됐다. compileall과 `git diff --check`도 통과했다.
- 초기 5인 독립 리뷰는 실경로 partial-fill 누락, 허위 exit 사실,
  kill-switch 부재, projector ORM 의존을 Important로 지적했고 수정했다.
  후속 재검토가 지적한 부분체결 transaction 분리, 재기동 order ID 재수화,
  잔량이 남은 LIQUIDATE_ALL의 completed 오기록, scheduler-dead producer도
  수정했다. order 단위 broker-reconciled 승격은 추가 실측 전에는 안전하게
  보류한다.
- 최종 패널 1차에서 새로 발견한 callback 저장 실패 무추적, pending 추가
  체결/장마감 수량 역행, 잔고 검증 전 full-entry 사건, terminal order 오귀속,
  kill-switch shutdown/process-restart 오신호를 회귀로 재현해 수정했다.
  scheduler-dead는 `scheduler_events.job VARCHAR(8)`에 9자 임시 값을 넣던
  PostgreSQL 결함을 제거하고 stable idempotency version으로 최대 3회
  비동기 재시도한다. loop와 dead-event DB worker를 별도로 소유해 shutdown은
  실제 worker terminal 뒤에만 engine 폐기로 진행한다.
- 실제 키움·Telegram API 및 운영 DB 호출은 하지 않았다.
- 최종 동일 구현 diff는 senior-developer, senior-trader,
  architecture-expert, security-expert, broker-api-expert가 독립 재검토해
  모두 Critical 0건, Important 0건으로 승인했다.

## Task 7 — durable outbox sender와 bounded retry

### 요청과 기존 상태

Task 6은 append-only operational event와 outbox 원천을 만들었지만 실제
Telegram delivery sender가 없었다. 초기 점검에서 projector가 outbox payload만
생성해 child delivery가 없는 실제 사건은 영구 pending으로 남는 단절도 함께
확인했다.

### 설계 판단과 변경 위치

- `core/telegram_service.py`의 `OutboxSender`는 한 delivery씩 90초 lease로
  claim한다. Bot API 35초 timeout보다 긴 lease이며 batch가 긴급 사건을
  점유하지 않는다. 429는 서버 `retry_after`, 5xx/transport는 bounded
  exponential backoff+jitter, 영구 오류/10회/24시간은 parent와 미전송
  sibling을 한 transaction에서 dead-letter로 끝낸다.
- `notification_store.py`는 parent lock, active sibling 및 earlier unsent
  `NOT EXISTS` 조건으로 같은 outbox 조각의 동시 전송과 backoff 중 조각이
  뒤의 긴급 outbox를 막는 starvation을 막는다. 성공한 민감 조각 body와
  payload는 즉시 NULL 처리한다.
- 5분 지난 CRITICAL은 `[지연 알림]`과 KST 실제 발생 시각을 첫 제목에
  붙인다. CRITICAL body는 이 제목을 예약한 4,032자 상한으로 생성 시점에
  검증해 최초의 상관 ID·`n/N` 고정 조각을 재시도 중 바꾸지 않는다.
- projector는 kind별 허용 scalar fact만 plain text로 렌더한다. token,
  account-like key, vendor error 원문 등 알려지지 않은 payload 값은 Telegram
  body로 승격하지 않는다. outbox/cursor/delivery는 한 transaction이며,
  Task 6 시점의 zero-child pending operational outbox는 parent lock 아래
  제한 batch로 backfill한다.

### 검증과 패널

- RED: sender 부재, 상태 전이, 지연 경계, legacy outbox, 민감 facts 누출
  회귀가 각 기능 부재 상태에서 실패함을 확인했다.
- focused 54건과 전체 1013 passed/11 deselected, compileall, diff check를
  확인했다. 기존 StarletteDeprecationWarning 1건만 남았다.
- 네 전문 관점의 반복 독립 검토에서 발생시각, lease, sibling race,
  critical body 경계, transaction backfill, 민감 렌더 경계를 보완했다.
  키움 경로는 변경하지 않아 broker-api-expert는 사용하지 않았다.
- 실제 Telegram/키움 API와 운영 DB는 호출하지 않았다.

## Task 8 — 16:10 다이제스트와 보존 maintenance

### 요청과 기존 상태

거래일 장 마감 뒤 당일 파이프라인·거래·계좌 상태를 한 번만 전달하고,
재기동으로 빠진 최근 날짜를 안전하게 캐치업해야 했다. 기존 outbox에는
sensitive query/digest TTL과 sender의 전송 후 scrub은 있었지만, 다이제스트
계획·원자 materialization·03:30 maintenance 조립은 없었다.

### 설계 판단과 변경 위치

- `DigestPlanner`는 KST 16:10 이전·비거래일에는 아무 날짜도 내지 않는다.
  최근 7거래일을 오래된 순서로 반환하며, 생성일의 최댓값이 아니라 각 날짜를
  outbox 존재 집합과 대조해 window 내부의 비연속 누락도 캐치업한다. window
  밖 gap만 `digest_skipped_stale` operational audit으로 종결한다. 날짜 멱등
  키는 `digest:{run_environment}:{YYYY-MM-DD}`다.
- brief의 `priority="low"`는 Task 4 이후 잘못된 계약이다. builder는
  `priority="digest"`를 사용해 fresh cache 또는 진행 중 interactive 조회만
  공유하며 broker 호출을 새로 시작하지 않는다. deferred·timeout·broker 실패는
  `None` 금액과 `account_snapshot` failed field로 정직하게 보존한다.
- `AccountSnapshotDeferred`는 core의 이름 문자열을 판별하지 않고
  `domain/notifications/ports.py`가 소유하는 명시 예외 계약으로 옮겼다.
  OperationsControl은 이를 re-export하므로 기존 호출자 호환도 유지한다.
- `NotificationStore.materialize_digest()`는 sensitive payload, 고정 delivery
  body, 24시간 purge 시각을 하나의 transaction에서 만들고 unique key가
  중복 생성을 차단한다. Task 7 formatter의 안정 상관 ID·고정 분할을 사용해
  Telegram body 상한을 넘지 않는다. `DigestRunStore`는 원문 경고나 vendor
  오류 대신 기준시각·상태·count만 가진 scalar read model을 만든다. digest
  planner와 materializer, run summary와 maintenance의 모든 동기 store 접근은
  `asyncio.to_thread` 경계에 둔다.
- maintenance는 만료 sensitive payload/body를 상태와 무관하게 scrub한 뒤,
  남은 batch 예산에서만 1년 지난 sent 메타데이터를 지운다. pending/sending
  active lease는 기존 Task 3 fence에 따라 보호한다. 03:30 metadata
  maintenance 외 sender tick도 만료 본문을 먼저 scrub해 TTL 노출을 다음날까지
  늘리지 않는다.
- adapter의 phase별 timeout만으로는 전체 외부 노출 상한이 되지 않는다.
  sender는 30초 `wait_for` 전체 deadline을 적용해 deadline 초과를 fenced
  retry로 끝내며, store는 35초 이상 남은 sensitive outbox만 claim한다. 두
  상수 관계도 생성 시 검증한다.

### 검증과 패널

- RED: digest 모듈 부재, maintenance 부재, BrokerError fallback 부재,
  환경 audit 충돌, 전 거래일 pipeline 기준일을 차례로 관측했다.
- focused 63건, 전체 1029 passed/11 deselected, `compileall`,
  `git diff --check`를 통과했다(기존 Starlette deprecation warning 1건).
- 1차 패널 Important(내부 hole, 환경 audit, typed read model/as_of, 본문
  상한·상관 ID, TTL·deadline)를 모두 수정했다. 최종 동일 diff는
  senior-developer, senior-trader, architecture-expert, security-expert가
  독립 재검토해 Critical 0건, Important 0건으로 승인했다. broker adapter/TR/
  주문 경로를 바꾸지 않아 broker-api-expert는 적용하지 않았다.
- 실제 Telegram·키움 API와 운영 DB는 호출하지 않았다. 브로커 adapter/TR/주문
  경로를 바꾸지 않아 broker-api-expert는 적용하지 않는다.

## Task 9 — 전체 Telegram 루프와 FastAPI lifespan 조립

### 요청과 기존 상태

Task 2~8의 설정/client/store/control/command/projector/sender/digest는 각각
검증됐지만, Bot API polling부터 명령 lane·outbox 전달까지 한 프로세스
수명으로 묶는 서비스가 없었다. 특히 poller DB lease, update offset과 감사의
원자 커밋, accepted command monitor와 sender lease의 종료 회수가 비어 있었다.

### 설계 판단과 변경 위치

- `core/telegram_service.py`에 `InboxPoller`, `CommandDispatcher`,
  `AsyncProjector`, `TelegramMaintenance`, `TelegramService`를 추가했다.
  poller는 40초 DB lease의 단일 승자만 long polling하며 허용 inbox,
  미허용 분 단위 bounded 집계, 다음 offset을 한 transaction으로 커밋한다.
- 외부 user/chat/subject 식별자는 bot token을 키로
  `HMAC-SHA-256(token, "v1:{kind}:{id}")`한 `v1:` 값만 저장한다. 이를 위해
  `telegram_common.py`, ORM의 관련 길이를 67자로 맞추고 기존 0013은
  보존한 채 새 0014 widening migration을 추가했다.
- query lane은 동시성 1이며 poll 입장 시 실제 backlog 20 상한을 적용한다.
  초과 query는 offset과 bounded 거부 집계를 함께 커밋하되 control은 계속
  입장한다. worker 준비
  전 행을 미리 claim하지 않아 느린 `/account`가 뒤 query lease를 만료시키지
  않는다. control lane은 query와 별도로 허용 control의 `update_id` 순서를
  지키며 받은 뒤 5초 초과를 snapshot 경고로 남긴다.
- 각 loop는 연속 실패 예산 3회를 독립 소비한다. 예산 소진은 해당 child만
  dead로 만들며 예외 문자열 대신 고정 오류 분류만 로그/snapshot에 남긴다.
  poller와 sender는 하나의 인증 circuit를 공유한다. 429/일시 오류는
  failure budget을 소비하지 않고 `retry_after`/지수 backoff한다.
- 일반 명령 응답은 durable outbox로 보내며, confirmation 원문만 최대 20개
  메모리 queue로 보내 DB·로그에 토큰을 남기지 않는다. 종료 전에 전송되지
  않으면 운영자가 위험 명령을 다시 요청해 새 토큰을 발급받는다.
- `main.py`만 전체 종료 순서를 소유한다. Telegram begin에서 poller와 신규
  command claim을 막은 뒤 scheduler, trading 순으로 기존 정책에 따라
  종료하고, 그 뒤 Telegram finish를 10초로 제한한다. finish는 accepted
  monitor를 중단해 running intent를 `unknown`으로 이관하고 projector
  checkpoint, sender drain, 남은 sender lease 반환을 수행한다.
- 정상 설정에서만 client/store/processor/projector/sender/digest/maintenance를
  조립한다. 설정 없음과 replay는 `app.state.telegram_service = None`이다.

### 검증

- RED는 신규 service import 부재와 lifespan 조립 표면 4건 부재로 확인했다.
- lifespan 16건, command/control 27건, migration 17건,
  전체 1,049건이 통과했고 11건은
  live test라 제외됐다. 기존 Starlette deprecation warning 1건만 남았다.
- compileall과 `git diff --check`도 통과했다.
- 실제 Telegram·키움 API와 운영 DB는 호출하지 않았다.
- 1차 패널은 응답 미전달·기존 0013 수정(Critical), 실제로 강제되지 않은
  query 상한, stale poller commit, deferred hot loop, 일시 장애 budget,
  종료 deadline, 상태 관측(Important)을 찾았다. 모두 회귀 테스트와 함께
  수정했다. 재검토에서 찾은 confirmation 소비 직후 pending crash, 응답
  materialization 복구, 정확한 토큰 TTL, commit lease 반환, 느린
  reconciliation의 control 차단, snapshot DB I/O, `/stop` 범위 안내도
  추가 수정했다.
- 최종 동일 diff는 senior-developer, senior-trader, architecture-expert,
  security-expert가 독립 재검토해 Critical 0건, Important 0건으로 승인했다.
  broker adapter/TR/주문 경로를 바꾸지 않아 broker-api-expert는 적용하지
  않았다.

## Task 10 — 비라이브 수용 준비·운영 문서·최종 회귀

### 요청과 기존 상태

Phase 8 구현 Task 1~9와 0014 migration이 커밋된 뒤, 비밀 비노출·외부 호출
차단 회귀, 현재 migration chain 검증, 운영자가 승인 뒤 수행할 모의 수용 절차,
재개 문서를 마무리했다. Task 10 시작 시 `docs/STATUS.md`는 Task 9가 미커밋인
것처럼 남아 있었고, 수용 brief도 예전 head 기준의 `0012 → 0013`만 적고 있었다.
실제 head는 `0012 → 0013 → 0014`이며, 운영 DB의 downgrade는 허용하지 않는다.

### Task 1~10 설계·변경 위치 요약

| Task | 경계와 핵심 판단 | 대표 변경 위치 |
|---:|---|---|
| 1 | Telegram DTO·DB 없이 명령/인증/평문 렌더링을 순수 domain으로 분리 | `backend/app/domain/notifications/{models,parsing,authorization,formatting}.py` |
| 2 | all-or-nothing 설정과 고정 origin Bot API client, URL·token 비노출 | `backend/app/core/config.py`, `backend/app/adapters/telegram/client.py` |
| 3 | inbox/confirmation/intent/event/outbox를 lease·CAS·고유키로 durable화 | `backend/alembic/versions/0013_telegram_notifications.py`, `backend/app/store/telegram_*_store.py`, `notification_store.py` |
| 4 | REST와 Telegram이 `OperationsControl`을 공유하고 managed liquidation만 허용 | `backend/app/core/operations_control.py`, `backend/app/api/{schedule,trade}.py` |
| 5 | 확인 소비와 command intent를 분리하고 crash 뒤 대사로 종결 | `backend/app/domain/notifications/commands.py`, `backend/app/store/telegram_command_store.py` |
| 6 | 거래/스케줄러 변경과 같은 transaction의 append-only operational event | `backend/app/store/{trading_store,scheduler_store}.py`, `backend/app/domain/trading/{service,monitor}.py` |
| 7 | 고정 delivery 조각, lease/retry/dead-letter, 민감 payload scrub | `backend/app/domain/notifications/projector.py`, `backend/app/store/notification_store.py` |
| 8 | 거래일 16:10 다이제스트, 7거래일 catch-up, bounded retention | `backend/app/domain/notifications/digest.py`, `backend/app/core/telegram_service.py` |
| 9 | 독립 loop/lane과 FastAPI lifespan 조립, HMAC hash widening | `backend/app/core/telegram_service.py`, `backend/app/main.py`, `backend/alembic/versions/0014_telegram_hmac_v1.py` |
| 10 | 예시 환경의 placeholder 강제, chain/수용/재개 문서 최신화 | `backend/tests/test_environment_example.py`, `.env.example`, `.superpowers/sdd/task-10-brief.md`, `docs/STATUS.md`, `docs/architecture/system-overview.md` |

Task 10의 환경 예시는 PostgreSQL 비밀번호와 Telegram token·허용 user/chat ID를
모두 빈 값으로만 둔다. `docker-compose.yml`은 빈 값·미설정 값을 알려진 기본값으로
fallback하지 않고 필수 변수 보간으로 fail-fast한다. 설명은 `.env`에만 충분히 긴
고유 비밀번호를 둘 것과 replay·테스트에서 서비스를 기동하지 않는다는 안전 계약을
유지한다. 새 회귀는 `.env.example`의 빈 값과 Compose의 약한 fallback 부재를 함께
확인한다. backend는 비밀번호를 URL user-info에 보간하지 않고 `PGPASSWORD`로
전달하므로 특수문자가 포함된 고유 비밀번호도 URL parsing으로 깨지지 않는다.

### TDD·마이그레이션·전체 검증

- RED: `cd backend && uv run pytest tests/test_environment_example.py -q`가
  예전 `POSTGRES_PASSWORD=ohmystock` 때문에 1건 실패했다.
- 두 번째 RED: 예시 문자열만 바꿔도 Compose의 `${POSTGRES_PASSWORD:-ohmystock}`
  fallback은 남는다는 정적 회귀가 실패했다.
- GREEN: `.env.example`을 빈 필수값으로 두고 Compose의 두 보간을
  `${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}`으로 바꿨다. 환경 예시,
  migration, Telegram lifespan focused 범위는 통과했다. backend는 password 없는
  `DATABASE_URL`과 `PGPASSWORD`를 분리한다.
- 로그 회귀: network-fake poller→dispatcher→실제 `CommandProcessor`
  (`/account`, `/resume` confirmation)→publisher 경로와 `MockTransport`
  `TelegramClient` redirect 오류를 통과시켰다. bot token, 생성 confirmation 원문,
  계좌 금액 sentinel은 `caplog`에 없고 confirmation만 memory sender에 전달된다.
  outbox에는 confirmation 원문을 적재하지 않는다.
- migration: 운영 DB가 아닌 경로를 `/tmp/ohmystock-p8-task10.*`로 검증한
  명시적 임시 SQLite DB에서 `0012 → 0013 → 0014`, `0014 → 0012`,
  `0012 → 0013 → 0014`를 실행했다. 양쪽 최종 version은 `0014`였고,
  파일은 `unlink`와 빈 디렉터리 `rmdir`로 제거했다.
- 전체(최신 동일 diff): `cd backend && uv run pytest -q`는 **1051 passed, 11 deselected**,
  기존 `StarletteDeprecationWarning` 1건으로 통과했다. 기본 pytest는 `live`
  marker를 제외하며, 일반 테스트 fixture가 Telegram 환경변수와 dotenv source를
  차단한다. Bot API 테스트는 mock transport, lifespan 테스트는 network 금지 fake를
  사용한다.

### 패널 발견과 수정

Task 1~9의 각 구현 단위는 senior-developer, senior-trader,
architecture-expert, security-expert의 독립 리뷰를 통과했다. 패널은 초기 단계에서
명령 응답 유실, confirmation·lease crash 창, partial fill/잔량의 허위 성공,
실제 backlog 상한 미강제, stale poller commit, sender retry·민감 본문 보존,
종료 소유권을 Critical/Important로 발견했다. 각각 durable intent와 reconciliation,
append-only event, CAS/version fence, poll 입구 상한, shared auth circuit,
scrub/retention, `main.py` 단일 종료 소유권과 회귀로 수정했다. Task 10의 1차
패널은 약한 Compose DB password fallback, URL user-info의 특수문자 처리, token을
실제 서비스 경로에 주입하지 않은 로그 회귀, STATUS의 중복 태스크 순서, 과거 Phase
상태 문구, 실제 모의 손절 수용 절차의 누락을 Critical/Important로 발견했다.
`.env.example` 빈 필수값과 Compose fail-fast, password 없는
`DATABASE_URL`+`PGPASSWORD`, network-fake poller→dispatcher→processor→publisher·
client 오류 caplog 회귀, 현재 재개점, `exit_reason=stop_loss` 별도 수용 게이트로
각각 수정했다.

최종 동일 diff는 `senior-developer`, `senior-trader`, `architecture-expert`,
`security-expert`가 다시 독립 검토해 **Critical 0건, Important 0건**으로
승인했다. 이번 단위는 키움 broker adapter/TR/주문 계약을 변경하지 않아
`broker-api-expert`는 대상이 아니다.

### 미실행 live·운영 한계·별도 수용 절차

이 태스크에서는 실제 Telegram, 키움 모의/실전 API, 운영 PostgreSQL, 별도 키움
토큰 발급을 **전혀 실행하지 않았다**. `live` marker 11건도 의도적으로 제외했다.
실제 수용에는 사용자 승인과 대상 bot/모의계좌의 명시가 필요하며, 별도 증거 파일에
비밀·계좌 원값을 기록하지 않는다.

승인 후에는 전용 테스트 bot과 개인 private chat을 `.env`에만 설정하고,
`KIWOOM_MOCK=true`, backend replica 1개, Uvicorn worker 1개로 기동한다. 다음을
순서대로 대사한다.

1. 허용/미허용·private/group 인증, `/status`·`/account`·`/positions`의 기준 시각과 마스킹을 확인한다.
2. `/pause`·`/stop` 즉시 효과와 `/resume`의 2단계 확인을 확인한다.
3. 장중 모의 환경에서 실제 진입 체결 뒤 **`exit_reason=stop_loss` 손절**을 반드시 유발하고, 별도로 킬스위치 청산 완료와 pipeline `gave_up`를 각각 유발한다. 각 경우의 outbox·Telegram 상관 ID·operational event·주문/잔량/포지션을 함께 대사한다. 일반 청산이나 `/liquidate_all`은 손절 검증을 대체하지 않으며, 이 증거는 아래 리플레이 결함 검증과 구분한다.
4. OhMyStock 관리 포지션만 대상으로 `/liquidate_all` preview/confirm을 수행하고 DB, broker 잔고, 미체결, intent terminal 상태를 함께 대사한다.
5. 리플레이 fault seam으로 부분체결·미체결·거래정지·장 종료를 재현해 잔량, 가격 정확도, 수동 조치와 `needs_attention` 알림을 DB/Telegram 양쪽에서 대사한다. 리플레이 결과를 실서버 실측으로 표현하지 않는다.
6. Telegram 차단·복구의 outbox/`[지연 알림]`, 발송 전후 재기동의 허용 중복 상관 ID, 16:10 다이제스트 날짜 멱등성을 확인한다.
7. SQL 감사와 로그에 token, confirmation 원문, 계좌번호, 금액 원값이 없는지 확인한다.

내장 Telegram 서비스는 backend/호스트 전체 다운을 스스로 알릴 수 없고, Bot API의
성공 응답과 DB 기록 사이 크래시에서는 동일 상관 ID의 at-least-once 중복 전송이
가능하다. pause는 기존 인메모리 계약이라 재기동 후 해제될 수 있으며,
`STOP_NEW_ENTRIES` 뒤 감시 승계 이슈와 재부팅 캐치업 검증은 Phase 8이 은폐하지
않는 별도 운영 한계다.
