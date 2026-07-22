import asyncio
import contextlib
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
                 sectors=None, market_symbols: dict[str, tuple[str, ...]] | None = None,
                 members: dict[tuple[str, str], list[str]] | None = None,
                 member_fail: dict[str, Exception] | None = None):
        self.symbols = list(symbols)
        self.fail = fail or set()
        self.candle_calls: list[str] = []
        self._sectors = sectors if sectors is not None else [
            Sector(code="013", market="kospi", name="전기전자")
        ]
        # market_symbols: 시장별 상장 종목을 세밀하게 제어하고 싶을 때만 지정.
        # 미지정 시 기존 동작(kospi만 self.symbols 반환, 그 외 시장은 빈 목록)을 유지.
        self._market_symbols = market_symbols
        # members: (sector_code, market) → 소속 심볼 목록을 세밀하게 제어하고
        # 싶을 때만 지정. 미지정 시 기존 동작(모든 업종이 self.symbols 전체를
        # 반환)을 유지.
        self._members = members
        # member_fail: sector_code → list_sector_members에서 던질 예외.
        # 특정 업종의 멤버십 조회 실패를 모사하고 싶을 때만 지정.
        self._member_fail = member_fail or {}

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
        if sector_code in self._member_fail:
            raise self._member_fail[sector_code]
        if self._members is not None:
            return list(self._members.get((sector_code, market), []))
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
        self.saved_memberships: dict[str, list[str]] = {}
        self.saved_group_types: dict[str, str] = {}
        self.candles: dict[str, list[Candle]] = {}
        self.runs: dict[int, dict] = {}
        self.replace_sector_memberships_calls = 0
        self._next = 1

    def upsert_sectors(self, sectors, group_types=None):
        self.saved_group_types = dict(group_types or {})
    def upsert_instruments(self, instruments):
        for i in instruments:
            self.instruments[i.symbol] = i
    def replace_sector_memberships(self, memberships):
        self.replace_sector_memberships_calls += 1
        self.saved_memberships = {code: list(members)
                                  for code, members in memberships.items()}
        return sum(len(members) for members in memberships.values())
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
    assert store.saved_memberships == {"013": ["005930", "000660"]}


@pytest.mark.anyio
async def test_create_run_이전에_running_placeholder를_세팅한다():
    """재실행 시 이전 런 최종 progress가 새 실행의 started_at/finished_at과
    뒤섞여 노출되는 tear를 방지 — create_run await 진입 시점에 progress가 이미
    running/starting이어야 한다(아키텍트 패널 P5-T1: analysis처럼 첫 _set이
    첫 await 이전). 실제 서브클래스의 갱신 순서를 검증한다(Fake 패스스루 아님)."""
    broker = FakeBroker()
    captured = []

    class SpyStore(MemoryStore):
        def create_run(self):
            captured.append(svc.progress())  # create_run 진입 시점 progress
            return super().create_run()

    svc = CollectionService(broker, SpyStore(), markets=("kospi",))
    await svc.run()
    assert captured and captured[0] is not None
    assert captured[0].status == "running" and captured[0].stage == "starting"
    assert captured[0].run_id is None  # create_run 이전이라 아직 None


@pytest.mark.anyio
async def test_재실행은_이미_최신인_종목을_건너뛴다():
    broker, store = FakeBroker(), MemoryStore()
    reference = date(2026, 7, 16)  # FakeBroker가 반환하는 봉 일자와 동일 — 결정론적 기준
    svc = CollectionService(broker, store, markets=("kospi",),
                            reference_provider=lambda: reference)
    await svc.run()
    first_calls = len(broker.candle_calls)
    await svc.run()
    # 두 번째 run: 전 종목의 최신 봉 일자가 기준일과 같거나 늦으므로 전부 스킵된다.
    assert len(broker.candle_calls) == first_calls


@pytest.mark.anyio
async def test_첫_종목이_낡은_봉만_반환해도_기준일이_오염되지_않는다():
    """A1 회귀 테스트. 과거 구현은 '이번 런 첫 성공 종목의 최신 봉 일자'를
    스킵 기준(reference_date)으로 삼았다 — 그 종목이 장기 거래정지라 낡은
    봉만 돌려주면 기준일 자체가 낡아져, 실제로는 갱신이 필요한 다른 종목까지
    "이미 최신"으로 오판해 영구 스킵되는 결함이 있었다. 지금은 달력 기준
    (reference_provider)을 쓰므로 첫 종목의 응답 내용과 무관하다."""
    class StaleFirstBroker(FakeBroker):
        async def get_daily_candles(self, symbol, count):
            self.candle_calls.append(symbol)
            if symbol == "000660":  # list_symbols 정렬상 첫 종목 — 거래정지 모사
                return [Candle(symbol=symbol, date=date(2026, 6, 17), open=1,
                               high=2, low=1, close=2, volume=10)]
            return [Candle(symbol=symbol, date=date(2026, 7, 16), open=1,
                           high=2, low=1, close=2, volume=10)]

    broker = StaleFirstBroker(symbols=("005930", "000660"))
    store = MemoryStore()
    store.instruments["005930"] = Instrument(symbol="005930", name="삼성전자",
                                             market="kospi", instrument_type="보통주")
    store.instruments["000660"] = Instrument(symbol="000660", name="SK하이닉스",
                                             market="kospi", instrument_type="보통주")
    # 005930은 이틀 전 봉까지만 저장돼 있어 기준일(오늘)보다 낡다 — 재수집 대상.
    store.candles["005930"] = [Candle(symbol="005930", date=date(2026, 7, 15),
                                      open=1, high=2, low=1, close=2, volume=10)]

    svc = CollectionService(broker, store, markets=("kospi",),
                            reference_provider=lambda: date(2026, 7, 17))
    await svc.run()

    # 첫 종목(000660)이 30일 낡은 봉만 반환했어도 005930은 스킵되지 않고
    # 실제로 재조회됐다 — 옛 구현이라면 000660의 낡은 응답이 기준일을
    # 오염시켜 005930이 "이미 최신"으로 오판돼 스킵됐을 것이다.
    assert "005930" in broker.candle_calls


@pytest.mark.anyio
async def test_공휴일_시나리오에서는_전_종목이_재수집된다():
    """reference_provider(달력 기준 근사)가 공휴일 등으로 실제 최신 거래일보다
    늦은 날짜를 반환해도, 전 종목의 저장된 최신 봉이 그 기준보다 낡으면
    스킵 없이 전부 재수집된다 — 결함이 아니라 멱등이라 안전(A1 문서화 그대로)."""
    broker, store = FakeBroker(), MemoryStore()
    store.instruments["005930"] = Instrument(symbol="005930", name="삼성전자",
                                             market="kospi", instrument_type="보통주")
    store.instruments["000660"] = Instrument(symbol="000660", name="SK하이닉스",
                                             market="kospi", instrument_type="보통주")
    day_before_yesterday = date(2026, 7, 15)
    yesterday = date(2026, 7, 16)
    for symbol in ("005930", "000660"):
        store.candles[symbol] = [Candle(symbol=symbol, date=day_before_yesterday,
                                        open=1, high=2, low=1, close=2, volume=10)]

    svc = CollectionService(broker, store, markets=("kospi",),
                            reference_provider=lambda: yesterday)
    await svc.run()

    assert set(broker.candle_calls) == {"005930", "000660"}  # 스킵 0건


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
async def test_전_그룹_멤버십_저장():
    """집계 업종 포함 모든 그룹의 소속이 그대로 저장된다 (필터 없음)."""
    sectors = [Sector("001", "kospi", "종합(KOSPI)"),
              Sector("005", "kospi", "음식료/담배")]
    members = {("001", "kospi"): ["A0001", "A0002"],
              ("005", "kospi"): ["A0001"]}
    broker = FakeBroker(sectors=sectors, members=members)
    store = MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    await svc.run()
    assert store.saved_memberships == {"001": ["A0001", "A0002"],
                                       "005": ["A0001"]}
    assert store.saved_group_types == {"001": "aggregate", "005": "industry"}


@pytest.mark.anyio
async def test_미지_업종코드_경고(caplog):
    """분류 맵에 없는 코드는 unclassified로 저장되고 경고 로그를 남긴다."""
    broker = FakeBroker(sectors=[Sector("777", "kospi", "신설업종")],
                        members={("777", "kospi"): ["A0001"]})
    store = MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    with caplog.at_level(logging.WARNING):
        await svc.run()
    assert store.saved_group_types == {"777": "unclassified"}
    assert any("unclassified sector" in r.message for r in caplog.records)


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
        def upsert_sectors(self, sectors, group_types=None):
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
async def test_start는_연속_호출시_두번째를_거부한다():
    """check(_running)~set(_running=True) 사이에 await가 없어 원자적이다 —
    create_task는 스케줄만 할 뿐 즉시 실행하지 않으므로, 첫 start() 직후
    (이벤트 루프에 양보하기 전) 두번째 start()를 호출해도 안전하게 None이
    반환되어야 한다."""
    broker, store = FakeBroker(), MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    first = svc.start()
    second = svc.start()
    assert first is not None
    assert second is None
    assert svc.current_task() is first
    await first
    assert svc.is_running() is False


@pytest.mark.anyio
async def test_start는_완료_후_재시작을_허용한다():
    broker, store = FakeBroker(), MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    await svc.start()
    assert svc.progress().status == "done"
    second = svc.start()
    assert second is not None
    await second
    assert svc.current_task() is second


@pytest.mark.anyio
async def test_업종_멤버십_조회_실패는_격리되고_교체를_건너뛴다():
    """한 업종의 list_sector_members가 BrokerError를 던져도 런 전체가 중단되지
    않는다 — 실패 업종만 격리되고, 부분 수집분으로 교체하면 실패 업종의 기존
    소속이 삭제되므로 replace 자체를 건너뛰어 직전 완전 스냅샷을 보존한다.
    candles 단계는 계속 실행되어야 한다."""
    sectors = [Sector("013", "kospi", "전기전자"), Sector("005", "kospi", "음식료/담배")]
    broker = FakeBroker(sectors=sectors,
                        member_fail={"013": BrokerError("boom 013")})
    store = MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    await svc.run()
    p = svc.progress()
    assert p.status == "done"
    assert broker.candle_calls  # candles 단계가 실행됐다
    assert store.replace_sector_memberships_calls == 0  # 교체를 건너뛰었다
    assert "NOT replaced" in store.runs[p.run_id]["error"]


@pytest.mark.anyio
async def test_업종_멤버십_조회_AuthError는_전체_중단():
    """AuthError는 업종 격리 대상이 아니라 기존 candles 단계와 동일하게
    시스템 장애로 취급해 런 전체를 중단해야 한다."""
    broker = FakeBroker(member_fail={"013": AuthError("token dead")})
    store = MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    await svc.run()
    assert svc.progress().status == "failed"


@pytest.mark.anyio
async def test_unclassified_경고가_런_결과로_전파된다():
    """분류 맵에 없는 업종 코드는 경고 로그뿐 아니라 런의 error_summary와
    진행상황 warning(GET /collect/status)에도 전파돼야 운영자가 알아챌 수 있다."""
    broker = FakeBroker(sectors=[Sector("777", "kospi", "신설업종")],
                        members={("777", "kospi"): ["A0001"]})
    store = MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    await svc.run()
    p = svc.progress()
    assert p.status == "done"
    assert "unclassified sector codes" in store.runs[p.run_id]["error"]
    assert "unclassified sector codes" in p.warning


@pytest.mark.anyio
async def test_conflict_check가_참이면_start는_None을_반환한다():
    """상호 배제는 도메인 계약 — 반대편(scoring) 실행 중이면 start()가
    거부된다. API의 409는 사용자 메시지용 1차 관문일 뿐, 여기가 실제
    방어선(T7 패널 트레이더/아키텍트)."""
    broker, store = FakeBroker(), MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",),
                            conflict_check=lambda: True)
    assert svc.start() is None
    assert svc.is_running() is False


@pytest.mark.anyio
async def test_conflict_check가_참이면_run은_RuntimeError():
    broker, store = FakeBroker(), MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",),
                            conflict_check=lambda: True)
    with pytest.raises(RuntimeError, match="conflicting run in progress"):
        await svc.run()


@pytest.mark.anyio
async def test_create_run이_실패해도_running_상태가_풀린다():
    """create_run이 try 블록 밖에 있으면 예외 시 finally가 실행되지 않아
    _running이 영구히 True로 고착된다 (T7 패널 아키텍트 발견)."""
    class ExplodingCreateRunStore(MemoryStore):
        def create_run(self):
            raise RuntimeError("db down")

    broker, store = FakeBroker(), ExplodingCreateRunStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    task = svc.start()
    with contextlib.suppress(RuntimeError):
        await task
    assert svc.is_running() is False


@pytest.mark.anyio
async def test_실행_중_run_오용은_경고를_지우지_않는다():
    """T7 패널 발견 결함의 회귀 테스트. 과거 `run()` 오버라이드는 가드
    (super().run()의 _running 검사) 통과 여부와 무관하게 `self._warning = None`을
    먼저 실행했다 — 이미 실행 중인 인스턴스에 `run()`을 오용하면(정상 API는
    start()) RuntimeError로 끝나기 전에 살아있는 런의 warning이 지워졌다.
    지금은 `_on_accepted()` 훅이 가드 통과가 확정된 뒤에만 `_warning`을
    갱신하므로, 거부되는 호출은 `_warning`을 건드리지 않는다."""
    release = asyncio.Event()

    class BlockingBroker(FakeBroker):
        async def list_instruments(self, market):
            await release.wait()
            return await super().list_instruments(market)

    broker, store = BlockingBroker(), MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    task = svc.start(warning="W")
    # instruments 단계 진입 시점(self._set 호출 직후)까지 이벤트 루프에 양보한다
    # — 그 지점에서 progress().warning이 "W"로 세팅돼 있어야 블로킹 지점에서
    # run() 오용을 시도할 수 있다.
    for _ in range(10):
        await asyncio.sleep(0)
        if svc.progress() is not None:
            break
    assert svc.progress() is not None
    assert svc.progress().warning == "W"

    with pytest.raises(RuntimeError):
        await svc.run()
    assert svc.progress().warning == "W"  # run() 오용이 살아있는 런의 경고를 지우지 않음

    release.set()
    await task
    assert svc.progress().warning == "W"


@pytest.mark.anyio
async def test_start_거부시_pending_warning이_잔류하지_않는다():
    """이미 실행 중인 인스턴스에 start(warning="X")를 재호출하면 거부(None)
    되고, 그 "X"가 `_pending_warning`에 잔류해 이후 정상 start()의 새 런
    warning으로 새어들지 않아야 한다."""
    release = asyncio.Event()

    class BlockingBroker(FakeBroker):
        async def list_instruments(self, market):
            await release.wait()
            return await super().list_instruments(market)

    broker, store = BlockingBroker(), MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    task = svc.start(warning="W")
    await asyncio.sleep(0)

    rejected = svc.start(warning="X")  # 이미 실행 중 — 거부
    assert rejected is None

    release.set()
    await task
    assert svc.progress().warning == "W"  # 거부된 "X"가 진행 중이던 런에 새지 않음

    second = svc.start()  # 새 런, warning 없음
    assert second is not None
    await second
    assert svc.progress().warning is None  # 거부됐던 "X"가 다음 런으로 새지 않음


@pytest.mark.anyio
async def test_start된_태스크의_미처리_예외는_done_callback이_로깅한다(caplog):
    class ExplodingStore(MemoryStore):
        def upsert_sectors(self, sectors, group_types=None):
            raise RuntimeError("db exploded")

    broker, store = FakeBroker(), ExplodingStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    with caplog.at_level(logging.ERROR):
        task = svc.start()
        with contextlib.suppress(RuntimeError):
            await task
        # done 콜백은 task가 끝난 직후 이벤트 루프 콜백 큐에서 실행되므로
        # 한 번 더 양보해 콜백이 실행될 기회를 준다.
        await asyncio.sleep(0)
    assert any("collection task failed" in r.message for r in caplog.records)
