"""토큰 레지스트리(스펙 §7) — 단일 활성 토큰 의미론 재현.

실측 계약(CLAUDE.md §5, Phase 2):
- 신규 발급이 기존 토큰을 무효화한다(앱키당 활성 토큰 1개 — 측정 정황).
- 무효 토큰의 TR 호출은 HTTP 401이 아니라 **HTTP 200 + return_code=3 +
  return_msg에 `[8005:Token이 유효하지 않습니다]`**(실측 원문 —
  tests/kiwoom/test_client.py의 캡처 기반 문구와 동일).

시크릿 무로그 계약(§7 보안): 이 모듈은 appkey/secretkey를 **받지도, 저장
하지도 않는다** — 검증 없이 즉시 폐기하는 것은 api 계층 핸들러의 책임이고,
레지스트리는 발급된 토큰 문자열만 상태로 가진다."""

import secrets
from datetime import datetime, timedelta

from replay.clock import KST

TOKEN_TTL = timedelta(hours=24)
INVALID_TOKEN_MSG = "인증에 실패했습니다[8005:Token이 유효하지 않습니다]"
INVALID_TOKEN_RC = 3


class TokenRegistry:
    """단일 활성 토큰(단일 테넌트 목). expires_dt는 벽시계 기준 절대 KST —
    실서버 계약(만료 재발급 로직 검증용). 재생 시계와 무관(§5 — 토큰 수명은
    실서버 인프라 동작이지 재생 데이터가 아니다)."""

    def __init__(self, wall_now) -> None:
        self._wall_now = wall_now          # () -> datetime (KST 권장)
        self._active: str | None = None
        self._expires_at: datetime | None = None
        self.superseded_count = 0          # 재발급으로 무효화된 횟수(관측용)

    def issue(self) -> tuple[str, str]:
        """(token, expires_dt[YYYYMMDDHHMMSS KST]) — 기존 토큰은 즉시 무효
        (실측: 두 번째 발급이 첫 토큰을 8005로 만든 Phase 2 사고 재현)."""
        if self._active is not None:
            self.superseded_count += 1
        token = secrets.token_urlsafe(24)
        expires = self._wall_now().astimezone(KST) + TOKEN_TTL
        self._active = token
        self._expires_at = expires
        return token, expires.strftime("%Y%m%d%H%M%S")

    def is_valid(self, token: str | None) -> bool:
        if not token or token != self._active:
            return False
        return self._wall_now().astimezone(KST) < self._expires_at

    def force_invalidate(self) -> None:
        """§9 "토큰 8005 무효화" 시나리오(관리 API 소비) — 활성 토큰을 즉시
        무효화해 다음 TR부터 8005를 유발한다(Phase 2의 '두 번째 프로세스가
        토큰을 뺏은' 사고를 임의 시점에 재현). 재발급으로 복구."""
        if self._active is not None:
            self.superseded_count += 1
        self._active = None
        self._expires_at = None

    def revoke(self, token: str) -> bool:
        """실측: /oauth2/revoke는 return_code 0(성공). 미지 토큰 revoke의
        실서버 rc는 미실측 — 성공으로 관용 처리하되 False 반환으로 관측만."""
        if token == self._active:
            self._active = None
            self._expires_at = None
            return True
        return False
