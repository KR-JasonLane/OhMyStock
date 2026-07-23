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
from enum import Enum

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


class DropKind(Enum):
    """드롭 분류(P6 §4-c — P5 정정): 래치 판정이 문자열 prefix 매칭이
    아니라 이 구조화 필드만 읽는다(P6 계획 리뷰 개발자 #3 — 사유 문구는
    사람용, 분류는 기계용으로 분리. enum은 models의 PositionState 등 닫힌
    집합 컨벤션과 일관 — P6-T2 개발자 Minor).

    STRATEGIC — 판정이 성립한 탈락(갭 가드·유니버스·유동성·슬롯/예산·보유
                중복·쿨다운): 재시도해도 같은 결론 → 진입 배치 래치 유지.
    TECHNICAL — 판정 재료 자체가 없던 탈락(시세/컨텍스트 부재): 판정
                미성립 → 진입 창 내 재시도 대상."""
    STRATEGIC = "strategic"
    TECHNICAL = "technical"


@dataclass(frozen=True)
class DroppedCandidate:
    """탈락 후보 — 사유는 실측값 포함(사후 "왜 안 샀나" 재구성 가능해야).
    Phase 8(텔레그램)/7(대시보드)이 이 표면을 소비할 수 있어 명명 필드로
    노출한다(익명 튜플 금지 — 개발자 R-패치). kind는 래치 판정 입력."""
    symbol: str
    reason: str
    kind: DropKind = DropKind.STRATEGIC


@dataclass(frozen=True)
class SelectionResult:
    """select_entries 결과 — plans + 탈락 사유(Task 8 라이브 결함 수정:
    갭 가드 탈락이 침묵이라 40분간 '왜 안 사는가'를 로그로 판별 불가 —
    침묵 금지 + 결정 #36). 표면화(warnings/로그)는 호출자(Task 7) 책임."""
    plans: tuple[EntryPlan, ...]
    dropped: tuple[DroppedCandidate, ...]


def _all_dropped(candidates: list[EntryCandidate],
                 reason: str) -> SelectionResult:
    """전역 조기 종료 — 후보 전원에게 동일 사유(빈 결과도 '왜'를 남긴다)."""
    return SelectionResult(
        plans=(), dropped=tuple(
            DroppedCandidate(c.symbol, reason) for c in candidates))


def select_entries(candidates: list[EntryCandidate], held_symbols: set[str],
                   available_krw: int, config: TradingConfig) -> SelectionResult:
    """진입 계획 산출. plans 순서 = 입력 순서(pick rank 유지), 탈락 후보는
    dropped에 (symbol, 실측값 포함 사유)로 반환(침묵 드랍 금지 — Task 8
    라이브 결함 수정).

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

    dropped: list[DroppedCandidate] = []
    free_slots = config.max_positions - len(held_symbols)
    if free_slots <= 0 or available_krw == 0:
        return _all_dropped(candidates, (
            f"no free slots (held {len(held_symbols)}/{config.max_positions})"
            if free_slots <= 0 else "available_krw is 0"))

    # 슬롯 예산 — 분모는 고정 max_positions(잔여 슬롯 수 아님): 이미 보유한
    # 슬롯의 예산을 신규 후보에 재배분하면 종목당 비중이 커진다.
    slot_krw = int(available_krw * config.max_capital_pct / 100) // config.max_positions
    if slot_krw <= 0:
        # ⚠️ 계좌 잔액 원값을 사유에 넣지 않는다(보안 R-패치 Important:
        # /trade/status는 무인증(§8-2 이월) — 원값 echo는 잔액 유출)
        return _all_dropped(candidates,
                            "slot budget is 0 — available funds below "
                            "one slot")
    buffer = 1 - config.commission_buy_pct / 100

    plans: list[EntryPlan] = []
    for cand in candidates:
        if len(plans) >= free_slots:
            # break가 아니라 continue — 잔여 후보 전원에게 사유를 남기기
            # 위함(plans 결과는 동일: 이 분기가 첫 판정이라 추가 평가 없음)
            dropped.append(DroppedCandidate(
                cand.symbol, f"free slots exhausted ({free_slots})"))
            continue
        if cand.symbol in held_symbols:
            dropped.append(DroppedCandidate(cand.symbol,
                                            "already held (§6-3.3)"))
            continue
        if cand.signal_price <= 0 or cand.current_price <= 0:
            # 가격 결측 = 판정 재료 부재(기술적) — 일시적 빈 quote/결측 봉이
            # 원인일 수 있어 재시도 대상(P6 §4-c ③, degenerate quote 전례)
            dropped.append(DroppedCandidate(
                            cand.symbol, f"price missing (signal {cand.signal_price:,}, "
                            f"current {cand.current_price:,})",
                            kind=DropKind.TECHNICAL))
            continue
        # 갭 가드 — 정수 bp 교차곱셈(exit_rules와 동일 원칙): float 나눗셈은
        # 정확한 경계(+3.00%)에서 3.0000000000000027 > 3.0으로 정상 후보를
        # 오제외한다(개발자 패널 Critical 실측 재현). 경계는 포함(<=허용).
        guard_bp = round(config.signal_gap_guard_pct * 100)
        if abs(cand.current_price - cand.signal_price) * 10_000 > \
                cand.signal_price * guard_bp:
            gap_pct = (cand.current_price - cand.signal_price) \
                / cand.signal_price * 100
            dropped.append(DroppedCandidate(
                            cand.symbol, f"gap guard: current {cand.current_price:,} vs "
                            f"signal {cand.signal_price:,} ({gap_pct:+.2f}% "
                            f"> ±{config.signal_gap_guard_pct:.2f}%)"))
            continue
        if not passes_universe(cand.audit_info, cand.state):
            dropped.append(DroppedCandidate(
                            cand.symbol, f"universe filter: audit={cand.audit_info!r} "
                            f"state={cand.state!r}"))
            continue
        if cand.avg_trading_value_krw < config.min_avg_trading_value_krw:
            dropped.append(DroppedCandidate(
                            cand.symbol, f"liquidity: avg value "
                            f"{cand.avg_trading_value_krw:,} < "
                            f"{config.min_avg_trading_value_krw:,}"))
            continue
        quantity = int(slot_krw * buffer) // cand.current_price
        if quantity <= 0:
            dropped.append(DroppedCandidate(
                            cand.symbol, f"slot budget {slot_krw:,} buys 0 shares at "
                            f"{cand.current_price:,}"))
            continue
        plans.append(EntryPlan(symbol=cand.symbol, name=cand.name,
                               market=cand.market, quantity=quantity,
                               budget_krw=slot_krw))
    return SelectionResult(plans=tuple(plans), dropped=tuple(dropped))
