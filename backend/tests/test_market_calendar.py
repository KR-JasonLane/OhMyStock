from datetime import date, datetime

from app.core.market_calendar import (KST, held_business_days, is_market_hours,
                                      is_trading_day, previous_weekday,
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


# --- Task 1B: 공휴일 테이블 + 신규 함수 ---

def test_is_trading_day_평일이면_참():
    assert is_trading_day(date(2026, 7, 22)) is True  # Wednesday


def test_is_trading_day_주말이면_거짓():
    assert is_trading_day(date(2026, 7, 18)) is False  # Saturday
    assert is_trading_day(date(2026, 7, 19)) is False  # Sunday


def test_is_trading_day_공휴일이면_거짓():
    assert is_trading_day(date(2026, 2, 16)) is False  # 설날 (월요일이지만 휴장)
    assert is_trading_day(date(2026, 5, 5)) is False   # 어린이날
    assert is_trading_day(date(2026, 12, 25)) is False  # 성탄절


def test_is_trading_day_지방선거일_제헌절_휴장():
    # 트레이더 패널 교차검증 — calendarlabs 미반영, 최근 입법
    assert is_trading_day(date(2026, 6, 3)) is False   # 제9회 지방선거일 (수)
    assert is_trading_day(date(2026, 7, 17)) is False  # 제헌절 부활 (금)


def test_is_trading_day_연말_12월31일_휴장():
    # 한국 증시 연말 휴장 — calendarlabs 미포함이나 관례로 추가
    assert is_trading_day(date(2026, 12, 31)) is False  # 목요일이지만 휴장
    assert is_trading_day(date(2026, 12, 30)) is True   # 마지막 거래일


def test_is_trading_day_테이블_없는_연도는_평일_근사():
    # 2099는 테이블에 없음 → 평일이면 거래일로 폴백
    assert is_trading_day(date(2099, 1, 5)) is True   # 평일(월)
    assert is_trading_day(date(2099, 1, 3)) is False  # 토요일


def test_is_trading_day_테이블_없는_연도는_경고_로그(caplog):
    import logging

    from app.core import market_calendar
    # alembic fileConfig(disable_existing_loggers=True 기본값)가 다른 테스트에서
    # 이 로거를 비활성화했을 수 있어 재활성화(기존 워크어라운드 관례 —
    # test_collection_service/test_api_security 참고).
    logging.getLogger("app.core.market_calendar").disabled = False
    market_calendar._warned_years.discard(2098)  # 테스트 격리(연도별 1회 캐시)
    with caplog.at_level(logging.WARNING, logger="app.core.market_calendar"):
        is_trading_day(date(2098, 3, 4))  # 평일, 테이블 없음
    assert any("2098" in r.message and "미등록" in r.message for r in caplog.records)


def test_is_market_hours_공휴일_장중이면_거짓():
    # 설날 당일 10시 — 평일 근사면 참이지만 공휴일 반영으로 거짓
    now = datetime(2026, 2, 16, 10, 0, tzinfo=KST)
    assert is_market_hours(now) is False


def test_held_business_days_당일이면_0():
    entry = date(2026, 7, 22)  # Wednesday
    now = datetime(2026, 7, 22, 14, 0, tzinfo=KST)
    assert held_business_days(entry, now) == 0


def test_held_business_days_다음_거래일이면_1():
    entry = date(2026, 7, 22)  # Wed
    now = datetime(2026, 7, 23, 10, 0, tzinfo=KST)  # Thu
    assert held_business_days(entry, now) == 1


def test_held_business_days_주말_건너뛴다():
    entry = date(2026, 7, 17)  # Friday
    now = datetime(2026, 7, 20, 10, 0, tzinfo=KST)  # Monday
    # 토·일 제외 → 월요일이 1거래일째
    assert held_business_days(entry, now) == 1


def test_held_business_days_공휴일_건너뛴다():
    # 설날 연휴(2/16~18) 낀 구간: 2/13(금) 진입 → 2/19(목)
    entry = date(2026, 2, 13)   # Friday
    now = datetime(2026, 2, 19, 10, 0, tzinfo=KST)  # Thursday
    # 2/14토 2/15일 2/16~18 설날 → 거래일은 2/19 하나뿐
    assert held_business_days(entry, now) == 1


def test_held_business_days_미래_진입일이면_0():
    entry = date(2026, 7, 25)
    now = datetime(2026, 7, 22, 10, 0, tzinfo=KST)
    assert held_business_days(entry, now) == 0
