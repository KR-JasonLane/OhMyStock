from datetime import date, datetime

from app.core.market_calendar import KST, is_market_hours, previous_weekday


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
