from datetime import datetime

from app.core.market_calendar import KST, is_market_hours


def test_is_market_hours_평일_장중이면_참():
    now = datetime(2026, 7, 20, 10, 0, tzinfo=KST)  # Monday
    assert is_market_hours(now) is True


def test_is_market_hours_평일_장마감후면_거짓():
    now = datetime(2026, 7, 20, 15, 30, tzinfo=KST)  # exactly close, exclusive
    assert is_market_hours(now) is False


def test_is_market_hours_주말이면_거짓():
    now = datetime(2026, 7, 18, 10, 0, tzinfo=KST)  # Saturday
    assert is_market_hours(now) is False
