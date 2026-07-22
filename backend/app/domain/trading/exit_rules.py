"""청산 판정(순수 함수) — 이 파일이 자금 손실과 직결되는 가장 치명적인 로직이다.
부수효과(폴링·주문 발행)와 물리적으로 분리해 경계값을 전수 테스트한다(스펙 §3).

판정 우선순위(스펙 §6-2): 0.보유기간 초과(최상위 강제 청산, 결정 #34) →
가격 판정 중에서는 1.손절 최우선 → 2.트레일링 → 3.고정 익절(트레일링 미활성
시만 — 결정 #29 v2). 손절·트레일링 동시 성립 시 손절 라벨(실행은 둘 다
시장가라 결과 동일 — §6-2 라벨링 계약).

**기준 가격은 전부 그로스(비용 미반영)** — 비용은 P&L 기록(costs.py)에만
반영한다(§6-2·§7 경계).

peak 갱신·트레일링 활성화 래치·2단계 폭 선형 보간이 전부 이 함수 안에서
계산돼 ExitEvaluation으로 반환된다 — monitor(Task 6b)는 저장만 한다
(개발자 패널: 트레일링 로직이 순수 테스트 범위 밖으로 새지 않게)."""

from app.domain.trading.config import TradingConfig
from app.domain.trading.models import ExitEvaluation, ExitReason


def _trailing_width_pct(peak_gain_pct: float, config: TradingConfig) -> float:
    """현재 트레일링 폭(%) — 활성화 직후 넓은 폭에서 안착 후 좁은 폭으로
    **선형 보간**(결정 #35 v3.1). 계단 함수는 전환점(+8%)에서 보호선이
    불연속 점프해 신고점 직후 정상 눌림목에 털리는 새 휘핑쏘를 만든다
    (트레이더 v3) — 보간으로 연속화한다.

      peak_gain <= activate           → wide (활성화 직전·직후 최대 여유)
      activate < peak_gain < widen_until → wide에서 narrow로 선형 축소
      peak_gain >= widen_until        → narrow (안착 후 타이트)
    """
    wide = config.trailing_stop_wide_pct
    narrow = config.trailing_stop_pct
    span = config.trailing_widen_until_pct - config.trailing_activate_pct
    if span <= 0:
        return narrow  # 보간 구간 0(activate==widen_until) — 즉시 좁은 폭
    if peak_gain_pct >= config.trailing_widen_until_pct:
        return narrow
    if peak_gain_pct <= config.trailing_activate_pct:
        return wide
    frac = (peak_gain_pct - config.trailing_activate_pct) / span
    return wide - (wide - narrow) * frac


def evaluate_exit(*, entry_price: int, current_price: int, peak_price: int,
                  trailing_active: bool, held_business_days: int,
                  config: TradingConfig) -> ExitEvaluation:
    """한 번의 시세 관측에 대한 청산 판정. 반환 계약은 ExitEvaluation(models).

    보유기간 경계 확정(Task 1B 트레이더 패널 이월): `held_business_days`는
    진입일 당일 0이므로 **진입일을 1일째로 센 세션 수 = held + 1**이다.
    `max_holding_days=10`이면 **진입일 포함 10번째 거래일의 첫 판정에서 강제
    청산**(`held + 1 >= max_holding_days`) — 오버나이트 갭 노출 최대 9회.
    held >= max(11번째 세션) 청산으로 읽으면 의도(결정 #34: 갭 누적 차단)보다
    하루치 갭을 더 떠안는다(트레이더 v2 지적) — 보수 방향을 택한다.

    입력 검증: 가격은 양수여야 한다(브로커 파싱 실수 방어 — 0/음수 현재가로
    손절이 오발동하는 것 방지, fail-loud)."""
    if entry_price <= 0 or current_price <= 0 or peak_price <= 0:
        raise ValueError(
            f"prices must be positive: entry={entry_price} "
            f"current={current_price} peak={peak_price}")
    if held_business_days < 0:
        raise ValueError(f"held_business_days must be >= 0: {held_business_days}")

    new_peak = max(peak_price, current_price)
    peak_gain_pct = (new_peak / entry_price - 1.0) * 100.0

    # ⚠️ 임계 비교는 전부 **정수 basis-point 교차 곱셈**이다 — 원 단위 정수
    # 가격에 float 임계(entry×1.1 등)를 곱하면 부동소수점 오차로 정확한
    # 경계(+10.00%)에서 판정이 어긋난다(구현 중 실측: 100000×1.1 =
    # 110000.00000000001 → 익절 미발동). pct→bp 변환은 round(±0.005%p 미만
    # 오차, 설정값 정밀도 안).
    #   current <= entry×(1−s%) ⇔ current×10000 <= entry×(10000−s_bp)
    entry_bp = entry_price
    activate_bp = round(config.trailing_activate_pct * 100)
    new_active = trailing_active or (
        new_peak * 10_000 >= entry_bp * (10_000 + activate_bp))

    reason: ExitReason | None = None
    if held_business_days + 1 >= config.max_holding_days:
        reason = ExitReason.MAX_HOLDING
    elif current_price * 10_000 <= entry_bp * (
            10_000 - round(config.stop_loss_pct * 100)):
        reason = ExitReason.STOP_LOSS
    elif new_active and current_price * 10_000 <= new_peak * (
            10_000 - round(_trailing_width_pct(peak_gain_pct, config) * 100)):
        reason = ExitReason.TRAILING_STOP
    elif not trailing_active and current_price * 10_000 >= entry_bp * (
            10_000 + round(config.take_profit_pct * 100)):
        # 백스톱 — 판정 기준은 **입력 trailing_active(이전 관측까지의 상태)**다.
        # new_active로 판정하면 +10%에 닿는 순간 peak_gain도 ≥ activate가 되어
        # 이 분기가 영원히 도달 불가가 된다(구현 중 발견). 폴링 간격 사이 급등이
        # 활성화 문턱(5%)을 건너뛰어 곧장 +10%에 닿은 경우: 트레일링 분기는
        # current==new_peak라 발동 불가 → 이 백스톱이 잡는다. 정상적 연속
        # 상승에서는 이전 틱에 이미 래치(입력 True)라 발동하지 않는다(§6-2-b).
        reason = ExitReason.TAKE_PROFIT
    return ExitEvaluation(reason=reason, new_peak=new_peak,
                          new_trailing_active=new_active)
