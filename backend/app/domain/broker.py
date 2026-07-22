"""브로커 포트와 도메인 모델. 이 모듈은 특정 증권사를 알지 못한다.

P5 Task 4에서 `OrderPort`를 **별도 Protocol로 신설**했다(스펙 §5, ISP —
개발자 패널): 주문 계약을 기존 `BrokerPort`에 합치면 시세만 필요한
CollectionService/ScoringService가 개념적으로 주문 실행 계약에 의존하게 된다.
`KiwoomBroker` 단일 구현체가 두 Protocol을 모두 만족한다(비용 0)."""

import enum
from dataclasses import dataclass
from datetime import date as date_
from decimal import Decimal
from typing import Protocol, runtime_checkable


class OrderStyle(enum.Enum):
    """주문 유형(브로커 중립). 키움 trde_tp 코드값("0"/"3" — G2 실측)으로의
    매핑은 어댑터 내부에만 존재한다. 기초 모듈(broker)이 소유하고 trading이
    재사용한다 — 기능 모듈(trading)에 기초 모듈이 의존하는 방향 역전 방지
    (P5-T4 아키텍트 패널)."""
    LIMIT = "limit"
    MARKET = "market"


class OrderSide(enum.Enum):
    """매매 방향. 키움은 방향을 TR 선택(kt10000 매수/kt10001 매도)으로 구분 —
    그 매핑도 어댑터 소관."""
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    market: str           # "kospi" | "kosdaq" | "etf"
    instrument_type: str  # 브로커가 주는 구분값 원문
    state: str = ""       # ka10099 state 원문 (예: "증거금100%|거래정지")
    audit_info: str = ""  # ka10099 auditInfo 원문 (예: "정상", "관리종목")


@dataclass(frozen=True)
class Sector:
    code: str
    market: str
    name: str


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    price: int              # 현재가 (원)
    change_rate: Decimal    # 등락율 (%)
    volume: int             # 누적 거래량


@dataclass(frozen=True)
class Candle:
    symbol: str
    date: date_
    open: int
    high: int
    low: int
    close: int
    volume: int

    def __post_init__(self) -> None:
        if not (self.open > 0 and self.high > 0 and self.low > 0 and self.close > 0
                and self.high >= max(self.open, self.close)
                and self.low <= min(self.open, self.close)):
            raise ValueError(f"invalid candle {self.symbol} {self.date}")


@dataclass(frozen=True)
class Deposit:
    total: int      # 예수금 (원)
    available: int  # 주문가능금액 (원)


@dataclass(frozen=True)
class Position:
    symbol: str
    name: str
    quantity: int
    avg_price: int      # 평균 매입가 (원) — 브로커가 원 단위로 반올림해 제공하는 값
    current_price: int  # 현재가 (원)
    eval_amount: int    # 평가금액 (원)


@dataclass(frozen=True)
class Balance:
    positions: tuple[Position, ...]
    total_eval: int        # 총평가금액 (원)
    total_profit: int      # 총평가손익 (원, 음수 가능)


@dataclass(frozen=True)
class MarketData:
    """다종목 감시용 시세 스냅샷 — 기존 `Quote` **합성** + 최우선 호가(개발자
    패널: 필드 중복 방지). bid/ask는 진입 지정가 산정(§6-3.6)에 필요 —
    G1 실측: ka10095 응답에 5단계 호가 포함(sel_1th_bid/buy_1th_bid),
    최우선은 sel_bid(매도호가)/buy_bid(매수호가)."""
    quote: Quote
    bid: int    # 최우선 매수호가 (buy_bid)
    ask: int    # 최우선 매도호가 (sel_bid)


def validate_symbol(symbol: str) -> str:
    """종목코드 형식 검증(6자리 ASCII 영숫자) — fail-loud. 자금 이동 경로
    (주문/취소/시세 조인)는 형식이 어긋난 심볼을 HTTP 호출 전에 차단한다
    (P5-T4 보안 패널: 파이프 조인 파라미터 스머글링·오발주 방어). 규칙은
    도메인이 소유하고 어댑터(_normalize_symbol)가 재사용한다."""
    if not (len(symbol) == 6 and symbol.isascii() and symbol.isalnum()):
        raise ValueError(f"unexpected symbol format: {symbol!r}")
    return symbol


@dataclass(frozen=True)
class OrderRequest:
    """주문 요청(브로커 중립). 키움 trde_tp 코드값("0"/"3" — G2 실측)으로의
    매핑은 어댑터 내부에만 존재한다(스펙 §5)."""
    symbol: str
    side: OrderSide
    style: OrderStyle
    quantity: int
    limit_price: int = 0   # LIMIT일 때만 사용(양수 필수), MARKET이면 0

    def __post_init__(self) -> None:
        validate_symbol(self.symbol)  # 실주문 경로 — 형식 오류는 발주 전 차단
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive: {self.quantity}")
        if self.style is OrderStyle.LIMIT and self.limit_price <= 0:
            raise ValueError("limit order requires positive limit_price")
        if self.style is OrderStyle.MARKET and self.limit_price != 0:
            raise ValueError("market order must not carry limit_price")


@dataclass(frozen=True)
class OrderAck:
    """주문 접수 응답 — 바디만(§9 보안: Authorization/토큰은 어느 계층에도
    없음). order_no는 G2 실측 필드(ord_no)."""
    order_no: str
    message: str    # return_msg (예: "모의투자 매수주문완료")


@dataclass(frozen=True)
class OpenOrder:
    """미체결 주문 1건 — ka10075 응답 행(G2 실측: 리스트 키 oso)."""
    order_no: str
    symbol: str
    side: OrderSide       # io_tp_nm 기반
    order_qty: int        # ord_qty
    unfilled_qty: int     # oso_qty
    order_price: int      # ord_pric
    # ord_stt 원문(예: "접수") — **표시/감사용**. 벤더 상태 문자열로 도메인
    # 분기를 만들지 말 것(reconcile 등은 존재 유무·수량으로 판정 — 아키텍트 패널)
    status: str


@runtime_checkable
class OrderPort(Protocol):
    """주문·감시 계약(P5 트레이딩 엔진 소비 — 스펙 §5). TradingService/
    EntryExecutor/PositionMonitor는 이 포트에만 의존한다."""

    async def get_quotes(self, symbols: list[str]) -> list[MarketData]:
        """다종목 시세+호가. 구현체는 일괄 조회 상한(키움 ka10095: 100종목/
        파이프 구분 — G1 실측)을 내부에서 처리하며 호출자는 그 차이를 모른다.
        결측 종목(브로커가 빈 행 반환)은 결과에서 제외된다 — 호출자는 요청
        수와 응답 수가 다를 수 있음을 전제해야 한다(조회 실패 ≠ 가격 불변)."""
        ...

    async def place_order(self, req: OrderRequest) -> OrderAck: ...

    async def cancel_order(self, order_no: str, symbol: str) -> OrderAck:
        """전량 취소(잔량 전부 — G2 실측 cncl_qty="0" 계약). 부분 취소는
        P5 비범위(부분체결 시 체결분 인정 + 잔량 전량 취소 — 스펙 §6-1)."""
        ...

    async def get_open_orders(self) -> list[OpenOrder]: ...


@runtime_checkable
class BrokerPort(Protocol):
    """브로커가 제공해야 하는 계약. 주문은 OrderPort(P5)로 분리 확장됐다."""

    async def get_quote(self, symbol: str) -> Quote: ...

    async def get_daily_candles(self, symbol: str, count: int) -> list[Candle]:
        """최근 count개 일봉을 과거→최신 순으로 반환한다.

        장중에 호출되면 마지막 봉은 당일 미확정 봉일 수 있다 — 확정 봉만 필요한
        소비자(스코어링 등)는 장마감 이후 데이터를 쓰거나 당일 봉을 제외해야 한다.

        반환 가격은 수정주가(액면분할·배당락 조정) 기준이다 — 구현체는 조정된
        가격만 반환해야 한다.
        """
        ...

    async def get_deposit(self) -> Deposit: ...

    async def get_balance(self) -> Balance: ...

    async def list_instruments(self, market: str) -> list[Instrument]:
        """시장별 상장 종목 목록. market: "kospi" | "kosdaq" | "etf"."""
        ...

    async def list_sectors(self) -> list[Sector]:
        """업종 코드표 (전 시장)."""
        ...

    async def list_sector_members(self, sector_code: str, market: str) -> list[str]:
        """해당 업종에 속한 종목코드 목록.

        market이 필수인 이유: 업종 코드 체계는 시장(코스피/코스닥)마다 별도로
        운영되므로, 업종코드만으로는 어느 시장의 업종인지 특정할 수 없다.
        호출자는 list_sectors()가 반환한 Sector.market 값을 그대로 넘기면 된다.
        브로커별 상세 근거는 각 어댑터 구현체의 주석을 참고할 것.
        """
        ...
