"""KRX 호가단위(틱) 반올림 — 순수 함수. entry(Task 6a)는 호출만 한다
(반올림 로직이 부수효과 코드에 묻히지 않게 분리 — 트레이더 패널, 계획서 §3).

호가단위 표(2023-01 KRX 개편, 유가증권/코스닥 공통). **20만~50만 구간 = 500원 —
판별 실측으로 확정**(2026-07-22, `.superpowers/sdd/p5-pregate-tick-probe.txt`):
250의 배수·500의 비배수 지정가(244,750)가 `RC4003 모의투자 호가단위 오류`로
거부됨(공식 문서 다수 — 대신·삼성·한화 공지 — 와 일치). 초안의 250원 표는
G3 시장가 체결가(272,750, 250 배수) 1건을 과신한 오류였다 —
**⚠️ mock 시장가 체결가는 호가 그리드를 준수하지 않는다**(매칭 엔진 근사 산출,
같은 실측에서 발견된 mock 특성). 대조군(끝자리 10원)도 거부돼 mock이 지정가
틱 검증을 수행함이 확인됨 — 판별 유효(broker-api 패널 Critical 해소).

ETF는 전 가격대 5원 — 증권사 공지(대신)로 문서 확인, mock 실측은 대기."""

# (하한가, 호가단위) — 하한 이상 구간에 해당 단위 적용. 내림차순 탐색.
_EQUITY_TICKS: tuple[tuple[int, int], ...] = (
    (500_000, 1_000),
    (200_000, 500),   # 판별 실측 확정(250 배수 지정가 RC4003 거부)
    (50_000, 100),
    (20_000, 50),
    (5_000, 10),
    (2_000, 5),
    (0, 1),
)

_ETF_TICK = 5  # 전 구간 5원 — 문서 확인(mock 실측 대기)


def tick_size(price: int, market: str) -> int:
    """해당 가격대의 호가단위. market은 "kospi"|"kosdaq"|"etf"."""
    if price <= 0:
        raise ValueError(f"price must be positive: {price}")
    if market == "etf":
        return _ETF_TICK
    if market not in ("kospi", "kosdaq"):
        raise ValueError(f"unknown market for tick: {market!r}")
    for floor, tick in _EQUITY_TICKS:
        if price >= floor:
            return tick
    raise AssertionError("unreachable")  # (0, 1)이 항상 매칭


def round_to_tick(price: int, market: str, direction: str) -> int:
    """가격을 유효 호가로 스냅. direction: "down"(매수 지정가 보수 방향 —
    더 낮게) | "up"(매도 지정가 보수 방향 — 더 높게).

    경계 주의: 스냅 결과가 구간 경계를 넘어 내려가는 경우(예: 200,100 down →
    200,000) 결과 가격대의 단위로도 유효한지 재확인한다 — 경계 바로 위 가격의
    내림이 하위 구간 값이 될 수 있기 때문."""
    if direction not in ("down", "up"):
        raise ValueError(f"direction must be down|up: {direction!r}")
    tick = tick_size(price, market)
    if direction == "down":
        snapped = (price // tick) * tick
    else:
        snapped = ((price + tick - 1) // tick) * tick
    # 현재 테이블 구조상 스냅 결과는 항상 결과 가격대에서도 유효하다 — 각 구간
    # 하한이 인접 구간 틱의 배수라 경계를 넘는 스냅이 정렬을 깨지 않는다
    # (개발자 패널: 이전의 '재스냅' 분기는 전 구간 스캔 결과 도달 불가 dead
    # code였음 — 제거). 표가 바뀌어 이 성질이 깨지면 조용히 무효 호가를 내는
    # 대신 여기서 fail-loud.
    if snapped > 0 and snapped % tick_size(snapped, market) != 0:
        raise AssertionError(
            f"tick table broke boundary alignment: {price} -> {snapped} ({market})")
    return snapped
