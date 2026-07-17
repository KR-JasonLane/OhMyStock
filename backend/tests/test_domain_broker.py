from datetime import date
from decimal import Decimal

from app.domain.broker import (
    Balance,
    BrokerPort,
    Candle,
    Deposit,
    Instrument,
    Position,
    Quote,
    Sector,
)


def test_모델은_불변이고_필드를_보존한다():
    q = Quote(symbol="005930", name="삼성전자", price=71000, change_rate=Decimal("1.25"), volume=1000)
    c = Candle(symbol="005930", date=date(2026, 7, 16), open=70500, high=71200,
               low=70100, close=71000, volume=999)
    d = Deposit(total=100_000, available=90_000)
    p = Position(symbol="005930", name="삼성전자", quantity=10, avg_price=69000,
                 current_price=71000, eval_amount=710_000)
    b = Balance(positions=(p,), total_eval=710_000, total_profit=20_000)
    assert q.price == 71000 and c.close == 71000 and d.available == 90_000
    assert b.positions[0].quantity == 10
    assert isinstance(b.positions, tuple)
    import pytest
    with pytest.raises(Exception):
        q.price = 0  # frozen


def test_BrokerPort는_런타임_프로토콜이다():
    class Fake:
        async def get_quote(self, symbol): ...
        async def get_daily_candles(self, symbol, count): ...
        async def get_deposit(self): ...
        async def get_balance(self): ...
        async def list_instruments(self, market): ...
        async def list_sectors(self): ...
        async def list_sector_members(self, sector_code, market): ...

    assert isinstance(Fake(), BrokerPort)
    assert not isinstance(object(), BrokerPort)


def test_Instrument와_Sector는_불변이고_필드를_보존한다():
    i = Instrument(symbol="005930", name="삼성전자", market="kospi", instrument_type="ST")
    s = Sector(code="013", market="kospi", name="전기전자")
    assert i.symbol == "005930" and i.market == "kospi"
    assert s.code == "013" and s.name == "전기전자"


def test_비정상_캔들은_생성시_ValueError():
    """Candle creation with invalid OHLC raises ValueError."""
    import pytest
    # high < close (invalid)
    with pytest.raises(ValueError, match="invalid candle"):
        Candle(symbol="005930", date=date(2026, 7, 16),
               open=70000, high=70100, low=69900, close=70500, volume=1000)
