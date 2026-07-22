"""evaluate_exit — 자금 리스크 핵심 판정의 경계값 전수 검증(스펙 §6-2).

기준 config: stop 5% / tp 10% / activate 5% / wide 5% / narrow 3% /
widen_until 8% / max_holding 10. entry=100,000으로 임계가 정수가 되게 한다."""

import pytest

from app.domain.trading.config import TradingConfig
from app.domain.trading.exit_rules import _trailing_width_pct, evaluate_exit
from app.domain.trading.models import ExitReason

CFG = TradingConfig(max_single_order_krw=10_000_000, max_daily_orders=50,
                    max_daily_order_krw=50_000_000, min_avg_trading_value_krw=0)
ENTRY = 100_000


def ev(current, *, peak=None, active=False, held=0, config=CFG):
    return evaluate_exit(entry_price=ENTRY, current_price=current,
                         peak_price=peak if peak is not None else max(ENTRY, current),
                         trailing_active=active, held_business_days=held,
                         config=config)


# --- 0순위: 보유기간 (Task 1B 이월 경계 확정) ---

def test_보유기간_진입일_포함_10번째_거래일에_청산():
    # held=9 → 진입일 포함 10세션째 → 강제 청산(오버나이트 최대 9회 — 보수 방향)
    assert ev(100_000, held=9).reason is ExitReason.MAX_HOLDING
    # held=8 → 9세션째 → 유지
    assert ev(100_000, held=8).reason is None


def test_보유기간이_손절보다_우선():
    # 동시 성립 시 0순위 라벨
    assert ev(90_000, held=9).reason is ExitReason.MAX_HOLDING


# --- 1순위: 손절 ---

def test_손절_정확_임계():
    assert ev(95_000).reason is ExitReason.STOP_LOSS   # 정확히 -5%
    assert ev(94_999).reason is ExitReason.STOP_LOSS
    assert ev(95_001).reason is None                    # 1원 위 — 유지


def test_손절이_트레일링보다_우선():
    # 고점 +8%(활성) 후 -5% 급락: 손절·트레일링 동시 성립 → 손절 라벨(§6-2)
    result = ev(95_000, peak=108_000, active=True)
    assert result.reason is ExitReason.STOP_LOSS


# --- 2순위: 트레일링 (활성화 래치 + 선형 보간 폭) ---

def test_활성화_래치_5퍼센트_돌파():
    r = ev(105_000)  # peak_gain 5.0% == activate → 래치 온
    assert r.new_trailing_active is True and r.reason is None
    assert ev(104_999).new_trailing_active is False


def test_래치는_한번_켜지면_유지():
    # 입력 active=True면 가격이 내려도 유지
    r = ev(101_000, peak=106_000, active=True)
    assert r.new_trailing_active is True


def test_트레일링_안착_후_좁은폭_3퍼센트():
    # peak +10%(≥ widen_until 8%) → narrow 3%: floor = 110,000×0.97 = 106,700
    assert ev(106_700, peak=110_000, active=True).reason is ExitReason.TRAILING_STOP
    assert ev(106_701, peak=110_000, active=True).reason is None


def test_트레일링_활성화_직후_넓은폭_5퍼센트():
    # peak +5%(== activate) → wide 5%: floor = 105,000×0.95 = 99,750
    # (넓은 폭 덕에 +5% 직후 3% 눌림(101,850)에는 안 털린다 — 결정 #35 취지)
    assert ev(101_850, peak=105_000, active=True).reason is None
    assert ev(99_750, peak=105_000, active=True).reason is ExitReason.TRAILING_STOP


def test_선형_보간_중간값():
    # peak +6.5% → 폭 = 5 − (5−3)×(6.5−5)/(8−5) = 4.0%
    assert _trailing_width_pct(6.5, CFG) == pytest.approx(4.0)
    # floor = 106,500×0.96 = 102,240
    assert ev(102_240, peak=106_500, active=True).reason is ExitReason.TRAILING_STOP
    assert ev(102_241, peak=106_500, active=True).reason is None


def test_전환점_불연속_없음():
    # widen_until(8%) 경계 전후로 폭이 연속(트레이더 v3 — 계단 휘핑쏘 제거)
    assert _trailing_width_pct(7.999, CFG) == pytest.approx(3.0, abs=0.01)
    assert _trailing_width_pct(8.0, CFG) == 3.0
    assert _trailing_width_pct(8.001, CFG) == 3.0


def test_보간_구간_0이면_즉시_좁은폭():
    cfg = TradingConfig(max_single_order_krw=10_000_000, max_daily_orders=50,
                        max_daily_order_krw=50_000_000,
                        min_avg_trading_value_krw=0,
                        trailing_widen_until_pct=5.0)  # == activate
    assert _trailing_width_pct(5.0, cfg) == cfg.trailing_stop_pct


# --- 3순위: 고정 익절 (백스톱 — 입력 active 기준) ---

def test_익절_백스톱_급등으로_활성화_건너뛴_경우():
    """폴링 사이 +5%를 건너뛰고 곧장 +10%: 트레일링은 current==peak라 발동
    불가 → 입력 active=False 기준 백스톱이 잡는다(구현 중 발견한 도달불가
    경로 수정의 회귀 고정 — new_active 기준이면 이 분기는 영원히 죽는다)."""
    r = ev(110_000)  # 입력 active=False, peak도 이번 틱 110,000
    assert r.reason is ExitReason.TAKE_PROFIT
    assert r.new_trailing_active is True  # 래치 자체는 켜진다


def test_정상_상승_경로에서는_익절_비발동():
    # 이전 틱에 +5% 래치(입력 True) → +10% 도달해도 익절 아님(결정 #29 v2 —
    # 트레일링만 상방 관리, 추세 계속 태움)
    r = ev(110_000, peak=110_000, active=True)
    assert r.reason is None


# --- peak 갱신 ---

def test_peak은_current로_단조_갱신():
    r = ev(112_000, peak=108_000, active=True)
    assert r.new_peak == 112_000
    r2 = ev(105_000, peak=108_000, active=True)
    assert r2.new_peak == 108_000  # 하락 시 유지


# --- 입력 검증 ---

@pytest.mark.parametrize("kwargs", [
    dict(entry_price=0, current_price=1, peak_price=1),
    dict(entry_price=1, current_price=0, peak_price=1),
    dict(entry_price=1, current_price=1, peak_price=-1),
])
def test_비양수_가격은_ValueError(kwargs):
    with pytest.raises(ValueError, match="prices"):
        evaluate_exit(trailing_active=False, held_business_days=0,
                      config=CFG, **kwargs)


def test_음수_보유일은_ValueError():
    with pytest.raises(ValueError, match="held_business_days"):
        ev(100_000, held=-1)
