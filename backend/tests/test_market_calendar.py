from datetime import date, datetime

from app.core.market_calendar import (KST, is_market_hours, previous_weekday,
                                      scoring_reference_date)


def test_is_market_hours_평일_장중이면_참():
    now = datetime(2026, 7, 20, 10, 0, tzinfo=KST)  # Monday
    assert is_market_hours(now) is True


def test_is_market_hours_평일_장마감후면_거짓():
    now = datetime(2026, 7, 20, 15, 30, tzinfo=KST)  # exactly close, exclusive
    assert is_market_hours(now) is False


def test_is_market_hours_주말이면_거짓():
    now = datetime(2026, 7, 18, 10, 0, tzinfo=KST)  # Saturday
    assert is_market_hours(now) is False


def test_previous_weekday_평일이면_오늘_그대로():
    now = datetime(2026, 7, 20, 10, 0, tzinfo=KST)  # Monday
    assert previous_weekday(now) == date(2026, 7, 20)


def test_previous_weekday_토요일이면_금요일로_보정():
    now = datetime(2026, 7, 18, 10, 0, tzinfo=KST)  # Saturday
    assert previous_weekday(now) == date(2026, 7, 17)


def test_previous_weekday_일요일이면_금요일로_보정():
    now = datetime(2026, 7, 19, 10, 0, tzinfo=KST)  # Sunday
    assert previous_weekday(now) == date(2026, 7, 17)


def test_scoring_reference_date_수요일_자정이면_화요일():
    now = datetime(2026, 7, 22, 0, 30, tzinfo=KST)  # Wednesday 00:30
    assert scoring_reference_date(now) == date(2026, 7, 21)  # Tuesday


def test_scoring_reference_date_월요일_자정이면_직전_금요일():
    now = datetime(2026, 7, 20, 0, 30, tzinfo=KST)  # Monday 00:30
    assert scoring_reference_date(now) == date(2026, 7, 17)  # previous Friday


def test_scoring_reference_date_토요일이면_금요일():
    now = datetime(2026, 7, 18, 10, 0, tzinfo=KST)  # Saturday
    assert scoring_reference_date(now) == date(2026, 7, 17)


def test_scoring_reference_date는_previous_weekday와_의미가_다르다():
    """스코어링 기준일은 항상 '오늘 이전' — 수집 재개 기준(previous_weekday,
    평일이면 오늘 포함)보다 하루 이상 앞선다."""
    wed = datetime(2026, 7, 22, 0, 30, tzinfo=KST)  # Wednesday
    assert scoring_reference_date(wed) < previous_weekday(wed)
