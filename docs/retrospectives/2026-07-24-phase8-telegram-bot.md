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
