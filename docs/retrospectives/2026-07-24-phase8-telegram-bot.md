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
- `/confirm`만 16~64자의 영숫자·`_`·`-` 토큰을 인자로 허용한다. 파서는
  토큰 문법과 해시 재료만 반환하며 저장·로그 기록은 후속 durable inbox의
  책임으로 남긴다.
- 인증은 user ID, chat ID, private chat, forwarded 아님을 모두 요구한다.
  운영자 컬렉션을 받아 초기 단일 운영자 정책과 향후 복수 운영자 확장을 함께
  지원한다.
- 텍스트는 Telegram 마크업을 지정하지 않고 plain text로만 렌더링한다. 각
  조각에 상관 ID와 순번을 붙여 at-least-once 전송의 중복 식별과 재조립을
  가능하게 했다. 상관 ID·순번·총 조각 수를 포함한 실제 header 길이를 매번
  반영해 모든 조각이 caller의 길이 한도 이하다. 빈 값·개행 상관 ID와 header를
  담지 못하는 한도는 명시적으로 거부한다.

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
- 전체: `cd backend && uv run pytest` 결과 797 passed, 11 deselected,
  기존 StarletteDeprecationWarning 1건이다. 외부 Telegram·키움 호출은 없다.
