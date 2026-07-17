import logging
from datetime import date

import pytest

from app.domain.broker import Candle, Instrument, Sector
from app.domain.collection import CollectionService
from app.domain.errors import AuthError, BrokerError


@pytest.fixture(autouse=True)
def _collection_logger_enabled():
    """alembic 마이그레이션 테스트(tests/store/test_models_migration.py)가
    fileConfig(disable_existing_loggers=True 기본값)로 alembic.ini를 로드하면,
    ini에 명시되지 않은 기존 로거(app.domain.collection 포함)가 세션 내내
    비활성화된다 — 테스트 실행 순서에 따라 caplog가 이 모듈의 로그를 못 잡는
    현상으로 나타남. 이 모듈의 로거만 명시적으로 재활성화해 순서 무관하게 만든다."""
    logging.getLogger("app.domain.collection").disabled = False


class FakeBroker:
    def __init__(self, symbols=("005930", "000660"), fail: set[str] | None = None,
                 sectors=None, market_symbols: dict[str, tuple[str, ...]] | None = None):
        self.symbols = list(symbols)
        self.fail = fail or set()
        self.candle_calls: list[str] = []
        self._sectors = sectors if sectors is not None else [
            Sector(code="013", market="kospi", name="전기전자")
        ]
        # market_symbols: 시장별 상장 종목을 세밀하게 제어하고 싶을 때만 지정.
        # 미지정 시 기존 동작(kospi만 self.symbols 반환, 그 외 시장은 빈 목록)을 유지.
        self._market_symbols = market_symbols

    async def list_instruments(self, market):
        if self._market_symbols is not None:
            return [Instrument(symbol=s, name=f"종목{s}", market=market,
                               instrument_type="보통주")
                    for s in self._market_symbols.get(market, ())]
        if market != "kospi":
            return []
        return [Instrument(symbol=s, name=f"종목{s}", market="kospi",
                           instrument_type="보통주") for s in self.symbols]

    async def list_sectors(self):
        return self._sectors

    async def list_sector_members(self, sector_code, market):
        return list(self.symbols)

    async def get_daily_candles(self, symbol, count):
        self.candle_calls.append(symbol)
        if symbol in self.fail:
            raise BrokerError(f"boom {symbol}")
        return [Candle(symbol=symbol, date=date(2026, 7, 16), open=1, high=2,
                       low=1, close=2, volume=10)]

    async def get_quote(self, symbol): ...
    async def get_deposit(self): ...
    async def get_balance(self): ...


class MemoryStore:
    def __init__(self):
        self.instruments: dict[str, Instrument] = {}
        self.sector_codes: dict[str, str] = {}
        self.candles: dict[str, list[Candle]] = {}
        self.runs: dict[int, dict] = {}
        self._next = 1

    def upsert_sectors(self, sectors): ...
    def upsert_instruments(self, instruments):
        for i in instruments:
            self.instruments[i.symbol] = i
    def set_sector_codes(self, mapping):
        # 실제 CollectionStore 계약과 동일: DB(=self.instruments)에 존재하는
        # 심볼만 반영하고, 반영된 개수를 반환한다.
        known = {s: c for s, c in mapping.items() if s in self.instruments}
        self.sector_codes.update(known)
        return len(known)
    def upsert_candles(self, candles):
        for c in candles:
            self.candles.setdefault(c.symbol, []).append(c)
    def latest_candle_date(self, symbol):
        rows = self.candles.get(symbol)
        return max(c.date for c in rows) if rows else None
    def latest_candle_dates(self):
        return {s: max(c.date for c in rows)
                for s, rows in self.candles.items() if rows}
    def list_symbols(self):
        return sorted(self.instruments)
    def create_run(self):
        rid = self._next; self._next += 1
        self.runs[rid] = {"status": "running"}
        return rid
    def finish_run(self, run_id, status, total, succeeded, failed, error_summary=None):
        self.runs[run_id] = {"status": status, "total": total,
                             "succeeded": succeeded, "failed": failed,
                             "error": error_summary}
    def deactivate_missing(self, seen_symbols):
        missing = [s for s in self.instruments if s not in seen_symbols]
        for s in missing:
            del self.instruments[s]
        return len(missing)


@pytest.mark.anyio
async def test_정상_수집은_전_단계를_완료한다():
    broker, store = FakeBroker(), MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    await svc.run()
    p = svc.progress()
    assert p.status == "done" and p.total == 2 and p.failed == 0
    assert store.runs[p.run_id]["succeeded"] == 2
    assert store.sector_codes == {"005930": "013", "000660": "013"}


@pytest.mark.anyio
async def test_재실행은_이미_최신인_종목을_건너뛴다():
    broker, store = FakeBroker(), MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    await svc.run()
    first_calls = len(broker.candle_calls)
    await svc.run()
    # 두 번째 run: 기준일 확보 전 첫 종목 1건만 재조회, 나머지는 스킵
    assert len(broker.candle_calls) <= first_calls + 1


@pytest.mark.anyio
async def test_종목_실패는_기록하고_계속한다():
    broker, store = FakeBroker(fail={"005930"}), MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    await svc.run()
    p = svc.progress()
    assert p.status == "done" and p.failed == 1
    assert store.runs[p.run_id]["succeeded"] == 1


@pytest.mark.anyio
async def test_연속_실패가_임계를_넘으면_run_failed():
    symbols = tuple(f"{i:06d}" for i in range(30))
    broker = FakeBroker(symbols=symbols, fail=set(symbols))
    store = MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",),
                            max_consecutive_failures=5)
    await svc.run()
    p = svc.progress()
    assert p.status == "failed"
    assert store.runs[p.run_id]["status"] == "failed"


@pytest.mark.anyio
async def test_인증_오류는_즉시_run_failed():
    class AuthFailBroker(FakeBroker):
        async def get_daily_candles(self, symbol, count):
            raise AuthError("token dead")
    broker, store = AuthFailBroker(), MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    await svc.run()
    assert svc.progress().status == "failed"


@pytest.mark.anyio
async def test_집계성_업종은_매핑에서_제외된다():
    sectors = [
        Sector(code="001", market="kospi", name="종합(KOSPI)"),
        Sector(code="013", market="kospi", name="전기전자"),
    ]
    broker, store = FakeBroker(sectors=sectors), MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    await svc.run()
    assert store.sector_codes == {"005930": "013", "000660": "013"}


@pytest.mark.anyio
async def test_전_시장_수집_성공시_이번_명부에_없는_종목은_비활성화된다():
    market_symbols = {"kospi": ("005930",), "kosdaq": ("000660",), "etf": ("069500",)}
    broker = FakeBroker(symbols=("005930", "000660", "069500"),
                        market_symbols=market_symbols)
    store = MemoryStore()
    svc = CollectionService(broker, store)  # 기본값 = 전 시장
    await svc.run()
    assert set(store.instruments) == {"005930", "000660", "069500"}

    # 두 번째 run: 000660이 명부에서 빠지고 신규 999999가 등장 — 모든 시장이
    # 여전히 비어있지 않으므로 비활성화 안전장치를 통과해야 한다.
    market_symbols2 = {"kospi": ("005930",), "kosdaq": ("999999",), "etf": ("069500",)}
    broker2 = FakeBroker(symbols=("005930", "999999", "069500"),
                         market_symbols=market_symbols2)
    svc2 = CollectionService(broker2, store)
    await svc2.run()
    assert set(store.instruments) == {"005930", "999999", "069500"}


@pytest.mark.anyio
async def test_한_시장이_빈_응답이면_비활성화를_건너뛴다(caplog):
    market_symbols = {"kospi": ("005930",), "kosdaq": (), "etf": ("069500",)}
    broker = FakeBroker(symbols=("005930", "069500"), market_symbols=market_symbols)
    store = MemoryStore()
    store.instruments["999999"] = Instrument(symbol="999999", name="기존종목",
                                             market="kosdaq", instrument_type="보통주")
    svc = CollectionService(broker, store)  # 기본값 = 전 시장
    with caplog.at_level(logging.WARNING):
        await svc.run()
    assert "999999" in store.instruments  # 비활성화가 스킵되어 그대로 남음
    assert any("skipping deactivation" in r.message for r in caplog.records)


@pytest.mark.anyio
async def test_부분_시장만_요청하면_비활성화를_건너뛴다(caplog):
    broker = FakeBroker()  # 기본: kospi만 종목 반환
    store = MemoryStore()
    store.instruments["999999"] = Instrument(symbol="999999", name="기존종목",
                                             market="kosdaq", instrument_type="보통주")
    svc = CollectionService(broker, store, markets=("kospi",))  # 부분 시장 요청
    with caplog.at_level(logging.WARNING):
        await svc.run()
    assert "999999" in store.instruments
    assert any("skipping deactivation" in r.message for r in caplog.records)


@pytest.mark.anyio
async def test_예상치_못한_예외도_run을_failed로_마감한다():
    class ExplodingStore(MemoryStore):
        def upsert_sectors(self, sectors):
            raise RuntimeError("db exploded")

    broker, store = FakeBroker(), ExplodingStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    with pytest.raises(RuntimeError):
        await svc.run()
    p = svc.progress()
    assert p.status == "failed"
    assert store.runs[p.run_id]["status"] == "failed"
    assert store.runs[p.run_id]["error"] == "unexpected: RuntimeError"


@pytest.mark.anyio
async def test_섹터_매핑_편중시_캐너리_경고를_남긴다(caplog):
    # 단일 업종(013)에 두 심볼이 모두 매핑 — 100% 편중이므로 캐너리 발동
    broker = FakeBroker(sectors=[Sector(code="013", market="kospi", name="전기전자")])
    store = MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    with caplog.at_level(logging.WARNING):
        await svc.run()
    assert any("sector mapping canary" in r.message for r in caplog.records)


def test_MemoryStore_set_sector_codes는_미존재_심볼을_건너뛴다():
    store = MemoryStore()
    store.instruments["005930"] = Instrument(symbol="005930", name="삼성전자",
                                             market="kospi", instrument_type="보통주")
    result = store.set_sector_codes({"005930": "001", "999999": "001"})
    assert result == 1
    assert store.sector_codes == {"005930": "001"}
