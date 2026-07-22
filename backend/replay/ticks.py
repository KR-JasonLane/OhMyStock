"""KRX 호가단위 — **app.domain.trading.ticks의 의도적 복제**(스펙 §4 확정:
목이 피검 코드를 임포트하면 같은 버그가 양쪽에 있어 검증이 자기참조로
무력화된다). 값 정합은 tests/test_ticks_parity.py가 **값 대조**(import가
아니라 상수 비교)로 강제 — 드리프트 시 테스트가 깨진다.

표 근거: 2023-01 KRX 개편 + 20만~50만 구간 500원 판별 실측
(.superpowers/sdd/p5-pregate-tick-probe.txt). ETF 전 구간 5원(문서 확인)."""

EQUITY_TICKS: tuple[tuple[int, int], ...] = (
    (500_000, 1_000),
    (200_000, 500),
    (50_000, 100),
    (20_000, 50),
    (5_000, 10),
    (2_000, 5),
    (0, 1),
)

ETF_TICK = 5


def tick_size(price: int, market: str) -> int:
    if price <= 0:
        raise ValueError(f"price must be positive: {price}")
    if market == "etf":
        return ETF_TICK
    if market not in ("kospi", "kosdaq"):
        # 원본(app.domain.trading.ticks)과 동일한 검증 분기 — 복제 누락 시
        # 오탈자 market이 조용히 주식 틱을 받는다(개발자 R2 #2)
        raise ValueError(f"unknown market for tick: {market!r}")
    for floor, tick in EQUITY_TICKS:
        if price >= floor:
            return tick
    raise AssertionError("unreachable")


def is_on_tick(price: int, market: str) -> bool:
    """지정가 틱 정렬 여부 — kt10000/kt10001의 RC4003 검증 재현(§7)에 사용."""
    return price > 0 and price % tick_size(price, market) == 0
