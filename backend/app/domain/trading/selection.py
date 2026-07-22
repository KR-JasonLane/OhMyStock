"""진입 후보 선정(순수 함수) — 어떤 종목을 몇 주 살지 결정한다(스펙 §6-3).
집행(발주·폴백)은 entry.py(Task 6a) 소관 — 판정/집행 분리(계획서 §3).

필터 체인(실행 순서 — 코드와 1:1, 개발자 패널 Minor 정합): 슬롯 잔여 확인 →
보유 중복 제외(§6-3.3) → 가격 결측 제외 → 갭 가드(§6-3.0) → 유니버스 필터
(§6-3.1) → 유동성(§6-3.2) → 사이징(§6-3.4-5: 분모=max_positions 고정, 수수료
버퍼, 내림, 0주 스킵). 각 필터는 독립 boolean이라 순서가 결과에 영향 없음.
입력 순서가 곧 우선순위(P4 pick rank) — 슬롯이 모자라면 앞선 후보부터 채운다.

순수성: DB·브로커를 모른다. avg_trading_value_krw(유동성)와 current_price는
호출자(Task 7 TradingService)가 조인/조회해 EntryCandidate로 주입한다
(계획서 — 순수 함수의 입력 경로 명시)."""

from dataclasses import dataclass

from app.domain.scoring.universe import passes_universe
from app.domain.trading.config import TradingConfig


@dataclass(frozen=True)
class EntryCandidate:
    """진입 후보 입력 — P4 최종 리스트(picks) 1행 + 조인된 시세/유동성.

    signal_price: P3 신호 기준가(스코어링 as-of 종가) — 갭 가드 비교 기준.
    avg_trading_value_krw: 저장 일봉(close×volume) 최근 N일 평균 —
    Task 7이 계산해 주입(스펙 §6-3.2 데이터 소스)."""
    symbol: str
    name: str
    market: str            # "kospi" | "kosdaq" | "etf"
    signal_price: int      # 신호 기준 종가 (P3 as-of)
    current_price: int     # 진입 직전 현재가
    audit_info: str        # ka10099 원문 (예: "정상")
    state: str             # ka10099 원문 (예: "증거금100%|거래정지")
    avg_trading_value_krw: int


@dataclass(frozen=True)
class EntryPlan:
    """선정 결과 — 6a EntryExecutor가 집행한다(지정가 산정은 6a에서 호가 기반)."""
    symbol: str
    name: str
    market: str
    quantity: int
    budget_krw: int        # 이 종목에 배정된 슬롯 예산(감사용)


def select_entries(candidates: list[EntryCandidate], held_symbols: set[str],
                   available_krw: int, config: TradingConfig) -> list[EntryPlan]:
    """진입 계획 산출. 반환 순서 = 입력 순서(pick rank 유지).

    사이징(§6-3.4, 결정 #26/#30 — 트레이더 v1 Critical 수정 반영):
      슬롯 예산 = available × max_capital_pct ÷ **max_positions(고정 분모)**.
      그날 후보 수가 아니라 고정 슬롯으로 나눠야 종목당 비중이 회차와 무관하게
      일정하다(후보 1개일 때 자금이 그 한 종목에 몰리는 결정 #30 붕괴 방지).
      후보가 슬롯보다 적으면 남는 예산은 현금으로 둔다.

    수수료 버퍼(§6-3.5): 수량 = 슬롯 × (1 − 매수수수료율) ÷ 현재가, **내림** —
    수수료 포함 시 주문가능금액 초과로 거부되는 것 방지.

    갭 가드(§6-3.0): |현재가/신호가 − 1| > signal_gap_guard_pct 이면 제외 —
    상방 갭(비싸게 삼)뿐 아니라 하방 갭(악재 신호)도 신호 전제 훼손이므로
    **양방향(절대값)** 판정(§11-3 취지 보수 해석).

    유니버스 필터(§6-3.1): scoring의 passes_universe 재사용(P3 §4-2 규칙과
    단일 출처) — 신호 생성(전날 밤)과 진입(아침) 사이 상태 변경 재확인.

    유동성(§6-3.2): avg_trading_value < min_avg_trading_value_krw 제외.
    임계 0 = 필터 비활성(config 문서 참조)."""
    if available_krw < 0:
        raise ValueError(f"available_krw must be >= 0: {available_krw}")

    free_slots = config.max_positions - len(held_symbols)
    if free_slots <= 0 or available_krw == 0:
        return []

    # 슬롯 예산 — 분모는 고정 max_positions(잔여 슬롯 수 아님): 이미 보유한
    # 슬롯의 예산을 신규 후보에 재배분하면 종목당 비중이 커진다.
    slot_krw = int(available_krw * config.max_capital_pct / 100) // config.max_positions
    if slot_krw <= 0:
        return []
    buffer = 1 - config.commission_buy_pct / 100

    plans: list[EntryPlan] = []
    for cand in candidates:
        if len(plans) >= free_slots:
            break
        if cand.symbol in held_symbols:
            continue  # 보유 중복 제외(§6-3.3)
        if cand.signal_price <= 0 or cand.current_price <= 0:
            continue  # 가격 결측 — 판정 불가 후보는 보수적으로 제외
        # 갭 가드 — 정수 bp 교차곱셈(exit_rules와 동일 원칙): float 나눗셈은
        # 정확한 경계(+3.00%)에서 3.0000000000000027 > 3.0으로 정상 후보를
        # 오제외한다(개발자 패널 Critical 실측 재현). 경계는 포함(<=허용).
        guard_bp = round(config.signal_gap_guard_pct * 100)
        if abs(cand.current_price - cand.signal_price) * 10_000 > \
                cand.signal_price * guard_bp:
            continue  # 갭 가드 — 신호 전제 훼손(양방향, §11-3 보수 해석)
        if not passes_universe(cand.audit_info, cand.state):
            continue  # 거래정지/관리종목 등
        if cand.avg_trading_value_krw < config.min_avg_trading_value_krw:
            continue  # 유동성 부족 — 시장가 폴백 슬리피지 위험
        quantity = int(slot_krw * buffer) // cand.current_price
        if quantity <= 0:
            continue  # 슬롯 예산으로 1주도 못 사는 고가주 스킵(§6-3.5)
        plans.append(EntryPlan(symbol=cand.symbol, name=cand.name,
                               market=cand.market, quantity=quantity,
                               budget_krw=slot_krw))
    return plans
