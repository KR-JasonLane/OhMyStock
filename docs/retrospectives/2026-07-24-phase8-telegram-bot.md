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
