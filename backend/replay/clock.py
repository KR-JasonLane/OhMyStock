"""리플레이 시계(스펙 §5) — 앵커 + 실경과×배속.

- replay_now()는 항상 **KST-aware**(§4-1 시계 tz 계약 — market_calendar
  정규화 버그의 교훈: naive/타 tz 시각이 판정에 새면 9시간이 어긋난다).
- 배속(speed)은 개발 편의 — 모든 소비자가 speed를 스탬프할 수 있도록 노출
  (§5 구조적 강제: speed≠1.0 런은 Task 8 근거 사용 금지)."""

import time as _time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


class ReplayClock:
    def __init__(self, anchor: datetime, speed: float = 1.0,
                 monotonic=None) -> None:
        """anchor: 재생 기준 시각. naive면 KST로 간주, aware면 KST로 변환.
        monotonic: 실경과 측정용 단조 시계(테스트 주입 — 벽시계 점프 무관)."""
        if speed <= 0:
            raise ValueError(f"speed must be positive: {speed}")
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=KST)
        self.anchor = anchor.astimezone(KST)
        self.speed = speed
        self._monotonic = monotonic or _time.monotonic
        self._start = self._monotonic()

    def now(self) -> datetime:
        """현재 재생 시각(KST-aware) = anchor + 실경과 × speed."""
        elapsed = self._monotonic() - self._start
        return self.anchor + timedelta(seconds=elapsed * self.speed)
