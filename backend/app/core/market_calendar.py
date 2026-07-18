"""KST 및 정규장 시간대 헬퍼. 프로젝트 전역에서 KST의 단일 출처.

휴장일(공휴일) 캘린더는 없다 — is_market_hours는 평일 09:00~15:30 근사치이며
advisory 용도(경고 목적)로만 쓴다. 실제 거래일 판정이 필요한 실행 강제는
Phase 6 스케줄러가 거래일 캘린더를 도입해 확장한다.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def is_market_hours(now: datetime | None = None) -> bool:
    """평일 09:00~15:30 KST 여부 (휴장일 캘린더 없는 근사 — advisory 용도)."""
    t = now or datetime.now(KST)
    return t.weekday() < 5 and time(9, 0) <= t.time() < time(15, 30)


def previous_weekday(now: datetime | None = None) -> date:
    """가장 최근의 평일 날짜 (오늘이 평일이면 오늘). 휴장일 캘린더 없는 근사 —
    실제 최신 거래일보다 늦은 날짜를 반환할 수 있는 경우(공휴일, 그리고 평일
    당일 봉이 아직 없는 시간대의 실행 등)에는 스킵이 풀려 전 종목을 재수집한다
    (멱등이라 안전, 비용만 증가). Phase 6 스케줄러가 거래일 캘린더와 야간 실행을
    강제하면 이 과잉 재수집 클래스는 소멸한다."""
    t = (now or datetime.now(KST)).date()
    while t.weekday() >= 5:
        t -= timedelta(days=1)
    return t


def scoring_reference_date(now: datetime | None = None) -> date:
    """오늘 이전(strictly before today)의 마지막 평일 — 스코어링 신선도 게이트 기준일.

    previous_weekday(수집 재개용 — 오늘이 평일이면 오늘)와 의미가 다르다:
    스코어링은 자정 배치라 '어젯밤 수집분(전 거래일 종가)'이 최신이며, 기준일이
    오늘이면 전 종목이 낡음 판정되어 매 자정 실행이 실패한다(T7 패널 트레이더
    Critical). 당일 저녁(수집 직후) 실행 시 하루 낡은 데이터가 게이트를 통과하는
    트레이드오프가 있으나 fail-safe 방향이고, Phase 6 스케줄러의 수집→스코어링
    순서 강제로 소멸한다. 휴장일 캘린더 없는 근사는 previous_weekday와 동일."""
    t = (now or datetime.now(KST)).date() - timedelta(days=1)
    while t.weekday() >= 5:
        t -= timedelta(days=1)
    return t
