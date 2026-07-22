"""트레이딩 도메인 모델 — 상태기계 enum + 주문/체결/포지션 dataclass.

별도 models.py를 두는 이유(계획서 §3): 상태기계 타입(PositionState/EntryPhase/
ExitPhase)이 entry/monitor/reconcile 여러 모듈에서 공유되므로 한 실행 모듈에
둘 수 없다(scoring/analysis와 다른 점 — 개발자 패널 Minor 승인).

`TradePosition`(엔진이 자체 추적하는 포지션, 상태기계 포함)과 기존
`domain/broker.py`의 `Position`(브로커 잔고 조회 응답 행)은 의도적으로 다른
타입이다 — 전자는 엔진의 ground truth 추적 상태, 후자는 브로커 보고값이며
reconcile(§6-6)이 둘을 대조한다.

가격/금액은 전부 원 단위 int — G3 실측(2026-07-22)으로 `pur_pric`이 원 단위
정수임을 확인(소수 없음), `TradePosition.avg_price: int` 가정 확정."""

import enum
from dataclasses import dataclass
from datetime import datetime


class OrderStyle(enum.Enum):
    """주문 유형(도메인 중립). 키움 `trde_tp` 코드값("0"/"3" — G2 실측, 단일자리)
    으로의 매핑은 어댑터 내부에만 존재한다(스펙 §5, 브로커 교체 가능성)."""
    LIMIT = "limit"
    MARKET = "market"


class OrderSide(enum.Enum):
    """매매 방향. 키움은 방향을 필드가 아니라 TR 선택(kt10000 매수/kt10001 매도)
    으로 구분한다 — 그 매핑도 어댑터 소관."""
    BUY = "buy"
    SELL = "sell"


class PositionState(enum.Enum):
    """포지션 수명주기(스펙 §6-1). EXIT_FAILED는 절대 조용히 넘기지 않는다 —
    재시도 소진 시 명시적 실패로 고정하고 상태 API에 노출(알람 대상)."""
    PENDING_ENTRY = "pending_entry"
    ENTERED = "entered"
    EXITING = "exiting"
    CLOSED = "closed"
    ENTRY_FAILED = "entry_failed"
    EXIT_FAILED = "exit_failed"


class EntryPhase(enum.Enum):
    """PENDING_ENTRY의 서브상태(스펙 §6-1) — DB 영속으로 재기동 복구 지점을
    식별한다(reconcile §6-6: CANCEL_REQUESTED에서 죽으면 고아 취소 → ENTRY_FAILED)."""
    LIMIT_SUBMITTED = "limit_submitted"
    CANCEL_REQUESTED = "cancel_requested"
    MARKET_SUBMITTED = "market_submitted"


class ExitPhase(enum.Enum):
    """EXITING의 서브상태 — **익절 지정가 경로에만 존재**(스펙 §6-2-b: 손절·
    트레일링·기간초과·킬스위치 청산은 시장가 단일이라 서브상태 없음).
    entry만 정교화하고 exit을 단일 화살표로 두면 이익 확정 경로에 같은 클래스
    결함이 남는다(개발자 패널 — v3 대칭화)."""
    LIMIT_SUBMITTED = "limit_submitted"
    MARKET_SUBMITTED = "market_submitted"


class ExitReason(enum.Enum):
    """청산 사유(스펙 §6-2 우선순위 순). 손절·트레일링 동시 성립 시 손절 라벨
    (실행은 둘 다 시장가라 결과 동일 — 라벨링 계약)."""
    MAX_HOLDING = "max_holding"      # 0순위 — 보유기간 초과 강제 청산(결정 #34)
    STOP_LOSS = "stop_loss"          # 1순위
    TRAILING_STOP = "trailing_stop"  # 2순위
    TAKE_PROFIT = "take_profit"      # 3순위 — 트레일링 미활성 시만(백스톱)
    KILL_SWITCH = "kill_switch"      # LIQUIDATE_ALL 킬스위치(§8-1-b)


@dataclass(frozen=True)
class ExitEvaluation:
    """`exit_rules.evaluate_exit`(Task 3)의 반환 계약 — monitor(Task 6b)와
    공유하는 SSOT를 코드 타입으로 못박는다(개발자 패널: prose 주석 계약은
    통합 시점에야 어긋남이 드러난다). monitor는 이 값을 **저장만** 한다."""
    reason: ExitReason | None      # None = 청산 조건 미충족(보유 유지)
    new_peak: int                  # max(peak, current) — 갱신된 고점
    new_trailing_active: bool      # 활성화 래치(한 번 True면 유지)


@dataclass(frozen=True)
class Order:
    """발주 이력 1건 — orders 테이블(§9)과 1:1. resp_body는 응답 **바디만**
    (Authorization 헤더/토큰은 어느 계층에도 남기지 않는다 — 보안 C1)."""
    order_no: str          # 브로커 주문번호 (ord_no — G2 실측)
    symbol: str
    side: OrderSide
    style: OrderStyle
    req_price: int         # 시장가면 0
    req_qty: int
    status: str            # submitted | cancelled | filled | rejected
    created_at: datetime

    def __post_init__(self) -> None:
        # 브로커 응답 파싱 실수(음수/0 수량 등)가 조용히 통과해 포지션 상태를
        # 오염시키지 않도록 최소 sanity check(개발자 패널 — config만 검증하고
        # 정작 금액 담는 모델이 무검증이던 비대칭 해소).
        if self.req_qty <= 0:
            raise ValueError(f"req_qty must be positive: {self.req_qty}")
        if self.req_price < 0:
            raise ValueError(f"req_price must be non-negative: {self.req_price}")


@dataclass(frozen=True)
class Fill:
    """체결 1건 — 부분체결 다건 가능(order:fills = 1:N, §9)."""
    order_no: str
    fill_price: int
    fill_qty: int
    filled_at: datetime

    def __post_init__(self) -> None:
        if self.fill_qty <= 0:
            raise ValueError(f"fill_qty must be positive: {self.fill_qty}")
        if self.fill_price <= 0:
            raise ValueError(f"fill_price must be positive: {self.fill_price}")


@dataclass(frozen=True)
class TradePosition:
    """엔진이 추적하는 포지션(상태기계 포함). peak_price/trailing_active는 DB
    영속 대상(§6-2 — 재시작 시 트레일링 기준 리셋 방지). 청산 판정 계약은
    `ExitEvaluation` 참조(monitor는 저장만 — 순수/부수 분리).

    market은 비용 계산(costs.round_trip_cost — 시장별 세율·ETF 면세)에 필수라
    포지션이 자기 시장을 안다(트레이더 패널: Task 6b에서 조달처 불명 방지).
    값은 기존 `Instrument.market`("kospi"|"kosdaq"|"etf") 그대로."""
    symbol: str
    name: str
    market: str                 # "kospi" | "kosdaq" | "etf" — 비용 계산용
    state: PositionState
    entry_price: int            # 평균 매입가 (원 단위 int — G3 실측 확정)
    quantity: int
    peak_price: int             # 보유 중 고점 (트레일링 기준)
    trailing_active: bool       # 활성화 래치 (한 번 True면 유지)
    entered_at: datetime | None = None
    entry_phase: EntryPhase | None = None   # PENDING_ENTRY에서만 의미
    exit_phase: ExitPhase | None = None     # EXITING 익절 지정가 경로에서만
    exit_price: int | None = None
    exit_reason: ExitReason | None = None
    realized_pnl: int | None = None         # 비용 반영 실현손익 (costs.py — §7)
    closed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive: {self.quantity}")
        if self.entry_price <= 0 or self.peak_price <= 0:
            raise ValueError(
                f"prices must be positive: entry={self.entry_price} peak={self.peak_price}")
        if self.peak_price < self.entry_price and self.state is PositionState.ENTERED:
            # peak는 진입가에서 시작해 단조 증가 — 진입가 미만이면 추적 오류
            raise ValueError(
                f"peak_price < entry_price: peak={self.peak_price} entry={self.entry_price}")
