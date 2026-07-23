"""KST 날짜 판정 공용 헬퍼(P6 Task 4) — "해당 KST 거래일에 시작한 run"
질의의 단일 구현.

⚠️ 왜 SQL DATE()가 아닌가(P6 계획 Task 4 개발자 Critical): run 타임스탬프는
UTC 저장이고 분석은 08:20 KST(= UTC 전날 23:20)에 돈다 — 변환 없이 날짜를
비교하면 정상 성공한 아침 런이 매일 "전날 런"으로 오분류돼 창 내 반복
재트리거(유료 LLM·뉴스 API 낭비 + 감사 오염)가 된다. SQL은 ±1일 여유의
거친 범위 프리필터만 하고(스캔 상한), 정확한 날짜 판정은 파이썬에서 KST
변환 후 수행한다(daily_order_usage와 동일 패턴 — trading_store 선례)."""

from datetime import date, datetime, timedelta, timezone

from app.core.market_calendar import KST


def coarse_utc_bounds(day: date) -> tuple[datetime, datetime]:
    """해당 KST 날짜를 포함하는 ±1일 여유의 UTC 범위(SQL 프리필터 전용 —
    정확 판정 아님)."""
    start_kst = datetime.combine(day, datetime.min.time(), tzinfo=KST)
    return ((start_kst - timedelta(days=1)).astimezone(timezone.utc),
            (start_kst + timedelta(days=2)).astimezone(timezone.utc))


def within_kst_day(dt: datetime | None, day: date) -> bool:
    """타임스탬프가 해당 KST 날짜에 속하는지 정확 판정. naive는 UTC 벽시계로
    간주(프로덕션 Postgres는 aware UTC — naive는 sqlite 테스트 경로뿐)."""
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST).date() == day
