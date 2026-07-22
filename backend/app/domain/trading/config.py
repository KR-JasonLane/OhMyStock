"""트레이딩 파라미터 단일 출처. 스펙 §6-2 표와 1:1 — 값 변경은 스펙 갱신과 함께.

AnalysisConfig(검증 없는 순수 dataclass)와 달리 `__post_init__`에서 fail-fast
검증한다(스펙 §6-2-c) — 이 값들은 손절가·주문 수량·감시 주기를 직접 결정하므로
검증 부재의 대가가 금전적이다(보안 패널). 버그 봉쇄 한도(단건/일일 주문 상한,
유동성 임계)는 기본값이 없다 — 명시적으로 정하지 않으면 기동 자체가 안 되는
하드 게이트(스펙 §8-1, "설정 필수").

비용 필드의 기본값은 모의서버 G3 실측(2026-07-22, 삼성전자 272,750원 1주:
pur_cmsn/sell_cmsn 각 950 ≈ 0.348%, tax 544 ≈ 매도대금의 0.2%) 기준이다 —
**모의 수수료는 실전(약 0.015%)보다 훨씬 높으므로 실전 전환 시 반드시 조정**
(CLAUDE.md §5). 코스닥 세율은 미실측이라 보수적으로 코스피와 동일 기본값을
쓴다(§4 재확인 항목)."""

import json
from dataclasses import asdict, dataclass
from datetime import time


@dataclass(frozen=True)
class TradingConfig:
    # --- 버그 봉쇄 한도 (기본값 없음 — 설정 필수, 스펙 §8-1) ---
    max_single_order_krw: int   # 단건 주문 절대 상한 (발주 직전 재검증)
    max_daily_orders: int       # 일일 주문 건수 상한 (초과 시 엔진 자동 정지)
    max_daily_order_krw: int    # 일일 주문 금액 상한
    # 유동성 필터 임계(최근 평균 거래대금). 0 = 필터 비활성화(명시적 선택 —
    # 모의 테스트 편의용, 실전에서는 저유동성 슬리피지 방지를 위해 양수 권장).
    min_avg_trading_value_krw: int

    # --- 청산 (스펙 §6-2, 결정 #29 v2/#34/#35) ---
    stop_loss_pct: float = 5.0            # 진입가 대비 손절
    take_profit_pct: float = 10.0         # 트레일링 미활성 구간에서만 (백스톱)
    trailing_activate_pct: float = 5.0    # 이 수익률 돌파 시 트레일링 래치 온
    trailing_stop_wide_pct: float = 5.0   # 활성화 직후 넓은 폭 (휘핑쏘 방지)
    trailing_widen_until_pct: float = 8.0  # 이 수익률까지 선형 보간으로 좁힘
    trailing_stop_pct: float = 3.0        # 안착 후 좁은 폭 (고점 대비)
    # 영업일 보유 상한 — N 설정 시 진입일 포함 N번째 거래일 첫 판정에서 강제
    # 청산(오버나이트 갭 노출 최대 N−1회 — 보수 방향, exit_rules docstring 참조)
    max_holding_days: int = 10

    # --- 진입 (스펙 §6-3, 결정 #26/#28/#30) ---
    max_positions: int = 5                # 사이징 분모 (고정 슬롯)
    max_capital_pct: float = 50.0         # 가용자금 대비 투입 상한
    signal_gap_guard_pct: float = 3.0     # 신호 기준가 대비 현재가 괴리 상한
    entry_window_start: time = time(9, 5)   # 진입 창 (시가 안정화 후)
    entry_window_end: time = time(9, 30)
    # 진입 지정가 = 최우선 매도호가(ask) − offset틱 (§6-3.6). 0 = ask 그대로
    # (즉시 체결 지향 + 가격 상한 통제 — 결정 #25 취지: 슬리피지 통제하되
    # 미체결 방치 방지). 양수면 더 유리한(낮은) 가격 — 체결 지연 트레이드오프.
    entry_tick_offset: int = 0
    limit_order_timeout_sec: float = 60.0  # 진입 지정가 미체결 → 시장가 폴백
    reentry_cooldown_min: int = 30        # 동일 종목 재진입 쿨다운

    # --- 감시/청산 집행 (스펙 §6-2-b/§6-4) ---
    exit_limit_timeout_sec: float = 5.0   # 익절 지정가 전용 (짧음 — 트레이더 패널)
    poll_interval_sec: float = 1.0        # ka10095 폴링 주기 (G1 실측 ~1초 정합)
    quote_failure_threshold: int = 5      # 연속 조회 실패 시 경고 전환

    # --- 매매 비용 (G3 실측 기반 모의 기본값 — 실전 전환 시 조정 필수) ---
    # ⚠️ 스펙 §7 명시 실전 세율과의 불일치(트레이더 패널, §4 재확인 등록):
    #   - 코스피: 스펙 실전 0.18%(거래세 0.03+농특세 0.15) vs 모의 실측 ≈0.20%
    #     — 2bp 차이, 원인 미규명(모의서버 자체 세율 or 세율 개정 반영 차).
    #   - 코스닥: 스펙은 "거래세만(농특세 없음)"이라 코스피와 달라야 하나
    #     미실측이라 보수적으로 동일 기본값. 실전 전환 게이트에서 공식 세율로
    #     반드시 재설정할 것.
    commission_buy_pct: float = 0.35      # 매수 위탁수수료 (모의 실측 ≈0.348%)
    commission_sell_pct: float = 0.35     # 매도 위탁수수료
    tax_sell_kospi_pct: float = 0.20      # 모의 실측 ≈0.2% (실전 0.18% — 위 주석)
    tax_sell_kosdaq_pct: float = 0.20     # 미실측 — 코스피와 동일 가정 (위 주석)

    def __post_init__(self) -> None:
        """fail-fast 검증(스펙 §6-2-c) — 잘못된 값은 기동 시점에 즉시 드러낸다."""
        errors: list[str] = []
        if not 0 < self.stop_loss_pct < 100:
            errors.append(f"stop_loss_pct는 (0,100): {self.stop_loss_pct}")
        if self.take_profit_pct <= 0:
            errors.append(f"take_profit_pct는 양수: {self.take_profit_pct}")
        if self.trailing_activate_pct < 0:
            errors.append(f"trailing_activate_pct는 0 이상: {self.trailing_activate_pct}")
        if not 0 < self.trailing_stop_pct <= self.trailing_stop_wide_pct < 100:
            errors.append(
                "trailing 폭은 0 < narrow <= wide < 100: "
                f"narrow={self.trailing_stop_pct} wide={self.trailing_stop_wide_pct}")
        if self.trailing_widen_until_pct < self.trailing_activate_pct:
            errors.append(
                "trailing_widen_until_pct는 activate 이상: "
                f"widen_until={self.trailing_widen_until_pct} "
                f"activate={self.trailing_activate_pct}")
        if self.take_profit_pct <= self.trailing_activate_pct:
            # 역전되면 트레일링이 켜지기 전에 고정 익절이 먼저 걸려 결정 #29 v2
            # ("트레일링 활성화 후 고정 익절 해제, 추세 추종")의 코드 경로가
            # 도달 불가능해진다 — 개별 범위 검증으로는 못 잡는 조합(트레이더 패널).
            errors.append(
                "take_profit_pct는 trailing_activate_pct보다 커야 함(결정 #29 v2): "
                f"tp={self.take_profit_pct} activate={self.trailing_activate_pct}")
        if self.max_holding_days < 1:
            errors.append(f"max_holding_days는 1 이상: {self.max_holding_days}")
        if self.max_positions < 1:
            errors.append(f"max_positions는 1 이상: {self.max_positions}")
        if not 0 < self.max_capital_pct <= 100:
            errors.append(f"max_capital_pct는 (0,100]: {self.max_capital_pct}")
        if self.signal_gap_guard_pct < 0:
            errors.append(f"signal_gap_guard_pct는 0 이상: {self.signal_gap_guard_pct}")
        if self.entry_window_start >= self.entry_window_end:
            errors.append(
                f"진입 창 시작 < 끝: {self.entry_window_start}~{self.entry_window_end}")
        if self.limit_order_timeout_sec <= 0 or self.exit_limit_timeout_sec <= 0:
            errors.append("주문 타임아웃은 양수")
        if self.poll_interval_sec <= 0:
            errors.append(f"poll_interval_sec는 양수: {self.poll_interval_sec}")
        if self.quote_failure_threshold < 1:
            errors.append(f"quote_failure_threshold는 1 이상: {self.quote_failure_threshold}")
        if self.reentry_cooldown_min < 0:
            errors.append(f"reentry_cooldown_min은 0 이상: {self.reentry_cooldown_min}")
        if self.entry_tick_offset < 0:
            errors.append(f"entry_tick_offset은 0 이상: {self.entry_tick_offset}")
        if self.max_single_order_krw <= 0:
            errors.append(f"max_single_order_krw는 양수: {self.max_single_order_krw}")
        if self.max_daily_orders < 1:
            errors.append(f"max_daily_orders는 1 이상: {self.max_daily_orders}")
        if self.max_daily_order_krw < self.max_single_order_krw:
            errors.append(
                "max_daily_order_krw는 단건 상한 이상: "
                f"daily={self.max_daily_order_krw} single={self.max_single_order_krw}")
        if self.min_avg_trading_value_krw < 0:
            errors.append(f"min_avg_trading_value_krw는 0 이상: {self.min_avg_trading_value_krw}")
        for name in ("commission_buy_pct", "commission_sell_pct",
                     "tax_sell_kospi_pct", "tax_sell_kosdaq_pct"):
            value = getattr(self, name)
            if not 0 <= value < 100:
                errors.append(f"{name}는 [0,100): {value}")
        if errors:
            raise ValueError("TradingConfig 검증 실패: " + "; ".join(errors))

    def to_json(self) -> str:
        """감사용 스냅샷(JSON) — trade_runs.config에 기록해 재현성 확보(§9)."""
        data = asdict(self)
        data["entry_window_start"] = self.entry_window_start.isoformat()
        data["entry_window_end"] = self.entry_window_end.isoformat()
        return json.dumps(data, sort_keys=True, ensure_ascii=False)
