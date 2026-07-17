"""브로커 포트와 도메인 모델. 이 모듈은 특정 증권사를 알지 못한다."""

from dataclasses import dataclass
from datetime import date as date_
from decimal import Decimal
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    market: str           # "kospi" | "kosdaq" | "etf"
    instrument_type: str  # 브로커가 주는 구분값 원문


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


@runtime_checkable
class BrokerPort(Protocol):
    """브로커가 제공해야 하는 계약. 주문/실시간은 Phase 5에서 확장한다."""

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
