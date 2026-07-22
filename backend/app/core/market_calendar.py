"""KST 및 정규장 시간대 헬퍼. 프로젝트 전역에서 KST의 단일 출처.

Phase 5(트레이딩 엔진)에서 정적 공휴일 테이블을 도입했다 — 실주문 경로(진입 창
판단·장 마감 판정·보유기간 계산)는 정확한 거래일 캘린더가 필요하기 때문(스펙
§4·§6-4). is_market_hours/is_trading_day/held_business_days가 이 테이블을 쓴다.

⚠️ 공휴일 테이블 한계 — 반드시 갱신 대상:
  - 출처는 3rd-party(calendarlabs) + 한국 증시 관례(연말 12/31 휴장)이며, KRX
    공식 발표로 재확인해야 한다(문서보다 실측 우선 원칙의 캘린더판 — 다만 미래
    날짜라 '실측' 불가, 공식 발표가 최선의 근거).
  - **임시공휴일(정부 지정, 통상 한두 달 전 공지)은 테이블에 없다** → 공지 시
    수동 갱신 필요(운영 절차).
  - **연 1회 갱신**: 다음 연도 테이블을 미리 추가한다. 테이블에 없는 연도는
    평일 근사로 폴백하되 `is_trading_day`가 연도별 1회 경고 로그를 남긴다
    (갱신 누락을 조용히 넘기지 않기 위함 — 트레이더/개발자 패널).
  - ⚠️ **추석 9/26(토) 대체휴일 → 9/28(월) 편입 여부 미확정**(출처 엇갈림) —
    Phase 6 전 KRX 공식 공고로 재확인.

수집/스코어링용 previous_weekday·scoring_reference_date는 기존 advisory 근사를
유지한다(Phase 6 스케줄러 통합 시 이 캘린더로 승격 — P5 비범위).
"""

import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
_logger = logging.getLogger(__name__)
_warned_years: set[int] = set()  # 테이블 미등록 연도 경고 중복 방지(연도별 1회)

# KRX 2026 휴장일 (출처: calendarlabs 3rd-party + 연말 12/31 휴장은 한국 증시 관례.
# KRX 공식 발표로 재확인·갱신 대상. 임시공휴일 미포함 — 공지 시 수동 추가).
_KRX_HOLIDAYS: dict[int, frozenset[date]] = {
    2026: frozenset({
        date(2026, 1, 1),    # 신정
        date(2026, 2, 16),   # 설날 연휴
        date(2026, 2, 17),   # 설날
        date(2026, 2, 18),   # 설날 연휴
        date(2026, 3, 2),    # 삼일절 대체휴일(3/1 일요일)
        date(2026, 5, 1),    # 근로자의날 (법정공휴일 아니나 KRX 휴장)
        date(2026, 5, 5),    # 어린이날
        date(2026, 5, 25),   # 부처님오신날 대체휴일(5/24 일요일)
        date(2026, 6, 3),    # 제9회 전국동시지방선거일 (법정공휴일 — 트레이더 패널 교차검증)
        date(2026, 7, 17),   # 제헌절 (2026 부활 — 2026-01-29 국회 통과, calendarlabs 미반영)
        date(2026, 8, 17),   # 광복절 대체휴일(8/15 토요일)
        date(2026, 9, 24),   # 추석 연휴
        date(2026, 9, 25),   # 추석
        date(2026, 10, 5),   # 개천절 대체휴일(10/3 토요일)
        date(2026, 10, 9),   # 한글날
        date(2026, 12, 25),  # 성탄절
        date(2026, 12, 31),  # 연말 휴장 (한국 증시 관례 — calendarlabs 미포함, 관례로 추가)
    }),
}


def _holidays_for(year: int) -> frozenset[date] | None:
    """해당 연도 휴장일 집합. 테이블에 없으면 None(호출자가 평일 근사로 폴백)."""
    return _KRX_HOLIDAYS.get(year)


def is_trading_day(d: date) -> bool:
    """거래일 여부 = 평일 and 공휴일 아님. 테이블에 없는 연도는 평일만 판정(폴백)
    하되 연도별 1회 경고 로그를 남긴다(갱신 누락 감지 — 실주문 안전장치)."""
    if d.weekday() >= 5:
        return False
    holidays = _holidays_for(d.year)
    if holidays is None:
        if d.year not in _warned_years:
            _warned_years.add(d.year)
            _logger.warning(
                "market_calendar: %d년 공휴일 테이블 미등록 — 평일 근사로 폴백"
                "(공휴일이 거래일로 오판될 수 있음, 테이블 갱신 필요)", d.year)
        return True
    return d not in holidays


def is_market_hours(now: datetime | None = None) -> bool:
    """거래일 09:00~15:30 KST 여부 (공휴일 테이블 반영). 트레이딩 엔진의 진입 창·
    장 마감 판정에 쓴다. 테이블에 없는 연도는 평일 근사로 폴백."""
    t = now or datetime.now(KST)
    return is_trading_day(t.date()) and time(9, 0) <= t.time() < time(15, 30)


def held_business_days(entry_date: date, now: datetime | None = None) -> int:
    """진입일(entry_date, 체결일)로부터 경과한 거래일 수 — max_holding_days 판정용
    (스펙 §6-2 결정 #34). 진입일 당일은 0, 다음 거래일부터 1씩 증가한다
    (entry_date 다음날 ~ 오늘 사이의 거래일 개수). 오늘이 진입일이면 0.

    예: 금요일 진입 → 다음 월요일(거래일)에 1. 연휴가 끼면 그만큼 느리게 증가.

    ⚠️ **경계 주의(트레이더 패널)**: 이 정의상 `held_business_days == N`은
    진입일을 포함해 총 N+1 세션이 지난 시점이다. 결정 #34의 "N영업일 보유
    상한"이 진입일을 1일째로 세는지, 그리고 임계 비교를 `>=` 로 할지 `>`로 할지는
    **Task 3 `evaluate_exit`에서 트레이더 의미를 확정**하고 경계값 테스트를 둔다
    (이 계약이 검증 없이 하류로 굳지 않도록)."""
    today = (now or datetime.now(KST)).date()
    if today <= entry_date:
        return 0
    count = 0
    d = entry_date + timedelta(days=1)
    while d <= today:
        if is_trading_day(d):
            count += 1
        d += timedelta(days=1)
    return count


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
