"""ticks — KRX 호가단위 스냅. 판별 실측(250 배수 지정가 RC4003 거부 → 500원 확정) 회귀 포함."""

import pytest

from app.domain.trading.ticks import round_to_tick, tick_size


@pytest.mark.parametrize("price,expected", [
    (1_999, 1), (2_000, 5), (4_999, 5), (5_000, 10), (19_999, 10),
    (20_000, 50), (49_999, 50), (50_000, 100), (199_999, 100),
    (200_000, 500), (499_999, 500), (500_000, 1_000), (1_000_000, 1_000),
])
def test_가격대별_호가단위(price, expected):
    assert tick_size(price, "kospi") == expected
    assert tick_size(price, "kosdaq") == expected  # 코스피/코스닥 공통


def test_판별_실측_확정_250배수는_무효_호가():
    """2026-07-22 판별 실측(p5-pregate-tick-probe.txt): 244,750(250 배수·
    500 비배수) 지정가가 RC4003(호가단위 오류)으로 거부 — 20만~50만 구간은
    500원. mock 시장가 체결가(272,750)는 호가 그리드 비준수(mock 특성) —
    체결가 1건으로 틱 표를 추정하지 말 것."""
    assert tick_size(244_750, "kospi") == 500
    assert 244_750 % 500 != 0  # 판별 가격은 500 그리드에서 무효 — 거부 실측과 정합


def test_etf는_전구간_5원():
    assert tick_size(1_000, "etf") == 5
    assert tick_size(300_000, "etf") == 5  # 문서 확인 — mock 실측 대기(모듈 주석)


def test_스냅_down_up():
    assert round_to_tick(272_749, "kospi", "down") == 272_500
    assert round_to_tick(272_749, "kospi", "up") == 273_000
    assert round_to_tick(272_500, "kospi", "down") == 272_500  # 이미 유효 — 멱등


def test_구간_경계_스냅():
    # 200,100 down: 500 단위 내림 → 200,000(경계값, 500 배수) — 유효
    assert round_to_tick(200_100, "kospi", "down") == 200_000
    # 499,999 up: 500 단위 올림 → 500,000 — 1,000원 구간 진입, 1,000 배수라 유효
    assert round_to_tick(499_999, "kospi", "up") == 500_000


def test_잘못된_입력():
    with pytest.raises(ValueError, match="price"):
        tick_size(0, "kospi")
    with pytest.raises(ValueError, match="unknown market"):
        tick_size(1_000, "nasdaq")
    with pytest.raises(ValueError, match="direction"):
        round_to_tick(1_000, "kospi", "nearest")
