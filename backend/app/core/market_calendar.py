"""KST 및 정규장 시간대 헬퍼. 프로젝트 전역에서 KST의 단일 출처.

휴장일(공휴일) 캘린더는 없다 — is_market_hours는 평일 09:00~15:30 근사치이며
advisory 용도(경고 목적)로만 쓴다. 실제 거래일 판정이 필요한 실행 강제는
Phase 6 스케줄러가 거래일 캘린더를 도입해 확장한다.
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def is_market_hours(now: datetime | None = None) -> bool:
    """평일 09:00~15:30 KST 여부 (휴장일 캘린더 없는 근사 — advisory 용도)."""
    t = now or datetime.now(KST)
    return t.weekday() < 5 and time(9, 0) <= t.time() < time(15, 30)
