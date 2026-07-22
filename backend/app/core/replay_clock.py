"""리플레이 오프셋 시계(스펙 §4-1) — 트레이딩 서비스 전용 주입 유닛.

replay 패키지의 ReplayClock(`anchor + 실경과 × speed`)과 대칭이지만
**speed 항이 의도적으로 없다(=1.0 고정 전제)**:

- 격리 제약(app↛replay — AST 회귀가 강제)으로 코드 재사용이 불가능해
  복제하되, 배속은 지원하지 않는다. 목 서버가 REPLAY_SPEED≠1.0으로 돌면
  이 시계와 목의 가상 시계가 조용히 어긋나 진입 창/장중 판정이 왜곡된다 —
  **speed≠1.0 런은 애초에 검증 근거 사용 금지**(§5 ③)이고 R7 게이트도
  speed=1.0 전제이므로, 여기서 배속을 흉내내지 않는 것이 계약이다.
- 배속 지원이 정말 필요해지면 Settings에 speed 필드를 추가하고 이 전제를
  갱신할 것(개발자 R6 #1 — 조용한 드리프트 금지: 전제는 코드에 남긴다).
"""

import time
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def make_replay_clock(anchor: datetime,
                      monotonic: Callable[[], float] | None = None,
                      ) -> Callable[[], datetime]:
    """`() -> KST-aware 재생 시각` — anchor + 실경과(monotonic), speed=1.0.
    naive anchor는 KST로 간주(§4-1 시계 tz 계약 — market_calendar와 동일)."""
    mono = monotonic or time.monotonic
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=KST)
    else:
        anchor = anchor.astimezone(KST)
    start = mono()

    def replay_now() -> datetime:
        return anchor + timedelta(seconds=mono() - start)

    return replay_now
