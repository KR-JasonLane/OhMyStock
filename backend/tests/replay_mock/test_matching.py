"""리플레이 R3(매칭 엔진) — 스펙 §8 룰·§5 미래 누출·FaultPolicy seam."""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from replay.account import Account
from replay.clock import KST
from replay.faults import FaultPolicy
from replay.matching import MatchingEngine
from replay.minute_store import MinuteStore

T0 = datetime(2026, 7, 10, 9, 0, tzinfo=KST)
WALL0 = datetime(2026, 7, 22, 20, 0, tzinfo=KST)  # 벽시계(전파 지연 판정)


def _row(ts: str, o: int, h: int, low: int, c: int, v: int = 10) -> dict:
    return {"cntr_tm": ts, "open_pric": f"+{o}", "high_pric": f"+{h}",
            "low_pric": f"+{low}", "cur_prc": f"+{c}", "trde_qty": str(v),
            "acc_trde_qty": str(v), "pred_pre": "0", "pred_pre_sig": "3"}


def _store(tmp_path: Path) -> MinuteStore:
    """09:00~09:03 분봉 — 09:00 100,000 → 09:01 하락(low 98,000) →
    09:02 상승(high 103,000) → 09:03 횡보."""
    db = tmp_path / "m.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS minute_raw(symbol TEXT, "
                 "page INTEGER, seq INTEGER, row TEXT, "
                 "PRIMARY KEY(symbol, seq))")  # 한 테스트 내 다중 Env 허용
    rows = [
        _row("20260710090000", 100_000, 100_500, 99_800, 100_000),
        _row("20260710090100", 100_000, 100_200, 98_000, 98_500),
        _row("20260710090200", 98_500, 103_000, 98_500, 102_500),
        _row("20260710090300", 102_500, 102_600, 102_300, 102_400),
    ]
    for seq, row in enumerate(rows):
        conn.execute("INSERT OR REPLACE INTO minute_raw VALUES ('005930', 0, ?, ?)",
                     (seq, json.dumps(row)))
    conn.commit()
    conn.close()
    return MinuteStore.load(db)


class Env:
    """조작 가능한 재생/벽 시계를 가진 매칭 하네스."""

    def __init__(self, tmp_path, cash=10_000_000, faults=None):
        self.replay_ts = T0
        self.wall_ts = WALL0
        self.account = Account(cash=cash)
        self.engine = MatchingEngine(self.account, _store(tmp_path),
                                     replay_now=lambda: self.replay_ts,
                                     wall_now=lambda: self.wall_ts,
                                     faults=faults)


def test_시장가는_현재가로_즉시_체결(tmp_path):
    env = Env(tmp_path)
    result = env.engine.submit("005930", "buy", "market", 10)
    assert result.ok
    assert env.account.holdings["005930"].quantity == 10
    assert env.account.holdings["005930"].avg_price == 100_000  # 09:00 close
    assert result.order_no not in env.account.open_orders  # 전량 체결 소멸


def test_지정가_매수_크로스면_즉시_현재가_체결(tmp_path):
    env = Env(tmp_path)
    result = env.engine.submit("005930", "buy", "limit", 10,
                               limit_price=100_500)
    assert result.ok
    # 체결가 = 현재가(더 유리) — limit 아님
    assert env.account.holdings["005930"].avg_price == 100_000


def test_지정가_매수_스탠딩은_현재가가_limit_이하로_내려오면_현재가로_체결(tmp_path):
    """트레이더 R3 #2 — check 시점 마켓터블 재평가(§8 갱신): 현재가(98,500)
    가 limit(99,000) 이하로 내려온 스탠딩 매수는 **현재가로** 체결."""
    env = Env(tmp_path)
    result = env.engine.submit("005930", "buy", "limit", 10,
                               limit_price=99_000)  # 현재가 100,000 미만
    assert result.ok and "005930" not in env.account.holdings
    env.engine.check_fills()          # 시각 진행 없음 — 여전히 미체결
    assert "005930" not in env.account.holdings
    env.replay_ts = T0 + timedelta(minutes=1, seconds=30)  # 현재가 98,500
    env.engine.check_fills()
    assert env.account.holdings["005930"].avg_price == 98_500  # 현재가 체결


def test_지정가_과거_구간_크로스는_limit가로_체결(tmp_path):
    """가격이 limit을 스치고 복귀한 경우(09:01 low 98,000 → 09:02 회복
    102,500) — check 시점 현재가는 비마켓터블이므로 과거 구간 크로스가
    limit가 체결을 만든다(§8 원 규칙 유지 확인)."""
    env = Env(tmp_path)
    result = env.engine.submit("005930", "buy", "limit", 10,
                               limit_price=99_000)
    assert result.ok
    env.replay_ts = T0 + timedelta(minutes=2)  # 현재가 102,500(비마켓터블)
    env.engine.check_fills()
    assert env.account.holdings["005930"].avg_price == 99_000  # limit가 체결


def test_미래_분봉은_시각_진행_전에_체결을_만들지_않는다(tmp_path):
    """§5 미래 누출 — 09:02 high 103,000이 존재해도 replay_now가 09:00이면
    지정가 매도(102,000)는 미체결이어야 한다."""
    env = Env(tmp_path)
    env.engine.submit("005930", "buy", "market", 10)
    result = env.engine.submit("005930", "sell", "limit", 10,
                               limit_price=102_000)
    assert result.ok
    env.engine.check_fills()
    assert env.account.holdings["005930"].quantity == 10  # 미체결(미래 미접근)
    env.replay_ts = T0 + timedelta(minutes=2)             # 09:02 high 103,000
    env.engine.check_fills()
    assert "005930" not in env.account.holdings           # limit 102,000 체결


def test_틱_위반_지정가는_RC4003_거부(tmp_path):
    env = Env(tmp_path)
    result = env.engine.submit("005930", "buy", "limit", 1,
                               limit_price=100_050)  # 10만 구간 틱 100
    assert not result.ok and "RC4003" in result.reason


def test_예수금_부족_매수와_보유_부족_매도는_거부(tmp_path):
    env = Env(tmp_path, cash=100)
    assert not env.engine.submit("005930", "buy", "market", 10).ok
    env2 = Env(tmp_path)
    assert not env2.engine.submit("005930", "sell", "market", 1).ok


def test_미체결_매도_수량은_이중_매도를_막는다(tmp_path):
    env = Env(tmp_path)
    env.engine.submit("005930", "buy", "market", 10)
    assert env.engine.submit("005930", "sell", "limit", 10,
                             limit_price=103_000).ok  # 전량 미체결 매도 등록
    # 같은 10주를 또 팔 수 없다(미체결 매도 예약 반영)
    assert not env.engine.submit("005930", "sell", "market", 10).ok


def test_취소는_미체결만_가능(tmp_path):
    env = Env(tmp_path)
    pending = env.engine.submit("005930", "buy", "limit", 10,
                                limit_price=99_000)
    assert env.engine.cancel(pending.order_no).ok
    filled = env.engine.submit("005930", "buy", "market", 1)
    result = env.engine.cancel(filled.order_no)
    assert not result.ok  # 체결 완료 취소 거부(가정 — 스펙 §7 PRE-GATE)


def test_전파_지연_동안_ka10075_비노출(tmp_path):
    env = Env(tmp_path)
    pending = env.engine.submit("005930", "buy", "limit", 10,
                                limit_price=99_000)
    assert env.account.visible_open_orders(env.wall_ts) == []  # C1 재현
    later = env.wall_ts + timedelta(seconds=2)
    visible = env.account.visible_open_orders(later)
    assert [o.order_no for o in visible] == [pending.order_no]


def test_fault_체결_억제와_부분체결_훅(tmp_path):
    class Partial(FaultPolicy):
        def __init__(self):
            self.suppressed = {"R0000002"}

        def suppress_fill(self, order_no):
            return order_no in self.suppressed

        def fill_quantity(self, order_no, quantity):
            return min(quantity, 4)  # 부분체결 시나리오

    faults = Partial()
    env = Env(tmp_path, faults=faults)
    first = env.engine.submit("005930", "buy", "market", 10)
    assert env.account.holdings["005930"].quantity == 4   # 부분체결(훅)
    assert env.account.open_orders[first.order_no].unfilled == 6
    second = env.engine.submit("005930", "buy", "market", 3)  # R0000002 억제
    assert second.ok
    assert env.account.open_orders[second.order_no].unfilled == 3  # 미체결 잔존
    faults.suppressed.clear()
    env.engine.check_fills()  # 억제 해제 — 시장가 잔존은 현재가로 체결
    assert second.order_no not in env.account.open_orders


def test_지정가_부분체결은_구간_내_이후_캔들로_잔량을_계속_채운다(tmp_path):
    """개발자 R3 Critical #1 회귀 — 부분체결에서 break+watch=now로 점프하면
    이미 관측된 크로스 캔들이 영구 유실된다. 잔량 소진까지 구간 스캔 지속."""
    class Partial(FaultPolicy):
        def fill_quantity(self, order_no, quantity):
            return min(quantity, 4)

    env = Env(tmp_path, faults=Partial())
    result = env.engine.submit("005930", "buy", "limit", 10,
                               limit_price=99_000)
    assert result.ok
    env.replay_ts = T0 + timedelta(minutes=3)  # 09:01/09:02 모두 크로스(low)
    env.engine.check_fills()
    # 한 번의 check_fills에서 두 크로스 캔들로 4+4=8 체결, 잔량 2
    assert env.account.holdings["005930"].quantity == 8
    assert env.account.open_orders[result.order_no].unfilled == 2


def test_취소_후_check_fills는_안전하고_재체결_없다(tmp_path):
    env = Env(tmp_path)
    pending = env.engine.submit("005930", "buy", "limit", 10,
                                limit_price=99_000)
    assert env.engine.cancel(pending.order_no).ok
    env.replay_ts = T0 + timedelta(minutes=2)  # 크로스 구간 진입
    env.engine.check_fills()                   # 예외 없음
    assert "005930" not in env.account.holdings  # 취소된 주문 재체결 금지


def test_매도_시장은_보유_포지션이_ground_truth(tmp_path):
    """아키텍트 R3 #4 — API 계층이 market 재전달을 누락해도 ETF 매도 틱
    검증이 보유 시장 기준으로 동작한다(40,005: ETF 틱 5 유효, kospi 틱 50
    위반 — 기본값이었다면 오거부)."""
    env = Env(tmp_path)
    env.engine.submit("005930", "buy", "market", 10, market="etf")
    result = env.engine.submit("005930", "sell", "limit", 5,
                               limit_price=100_005)  # market 파라미터 생략
    assert result.ok  # ETF 틱(5원) 기준 유효 — kospi(100원)였다면 RC4003


def test_미지_market_값은_거부(tmp_path):
    env = Env(tmp_path)
    assert not env.engine.submit("005930", "buy", "market", 1,
                                 market="Etf").ok  # 오타 침묵 통과 금지


def test_미체결_매수_예약이_이중_자금_사용을_막는다(tmp_path):
    """트레이더 R3 #1 — 실서버는 접수 시점에 ord_alow_amt를 차감한다: 같은
    자금을 겨냥한 두 번째 매수는 접수 거부돼야 중복 진입 버그가 리플레이
    에서 정상처럼 보이지 않는다(§2 갱신 — 예약 근사 재현)."""
    env = Env(tmp_path, cash=1_010_000)  # 10주(약 100만+수수료) 1건분만
    a = env.engine.submit("005930", "buy", "limit", 10, limit_price=99_500)
    b = env.engine.submit("005930", "buy", "limit", 10, limit_price=99_000)
    assert a.ok and not b.ok             # 예약 차감 — 실서버 재현
    assert "insufficient cash" in b.reason


def test_시장가_미체결도_접수가로_예약된다(tmp_path):
    """개발자 R3 델타 #2 — 시장가 미체결(억제 시나리오)의 예약 단가는 접수
    시점에 고정(reserve_price): 이후 시세 결측이 예약액을 침묵 0원으로
    만들어 이중 자금 사용이 재발하는 경로가 구조적으로 없다."""
    class Suppress(FaultPolicy):
        def suppress_fill(self, order_no):
            return True

    env = Env(tmp_path, cash=1_010_000, faults=Suppress())
    first = env.engine.submit("005930", "buy", "market", 10)  # 억제 — 미체결
    assert first.ok
    assert env.account.open_orders[first.order_no].reserve_price == 100_000
    second = env.engine.submit("005930", "buy", "market", 10)
    assert not second.ok and "insufficient cash" in second.reason


def test_음수_예수금은_카운터로_가시화된다():
    """아키텍트 R3 #5 잔여 — 만에 하나 음수 예수금이 나면(시장가 미체결
    예약액의 현재가 근사 한계 등) 침묵 대신 카운터+경고(§2: 카운터≠0인
    재생 결과는 kt00001 검증 근거 사용 금지)."""
    account = Account(cash=1_000)
    account.apply_buy_fill("005930", "kospi", 1, 100_000)
    assert account.cash < 0
    assert account.negative_cash_events == 1


def test_candles_between은_now_바인딩시_미래_until을_클램프(tmp_path):
    """아키텍트 R3 #2 — 미래 누출 방어를 호출 규율이 아니라 구조로."""
    store = _store(tmp_path)
    store.now_provider = lambda: T0  # 재생 시계 바인딩(R4 조립 계약)
    far_future = T0 + timedelta(days=1)
    assert store.candles_between("005930", T0 - timedelta(minutes=1),
                                 far_future) == \
        store.candles_between("005930", T0 - timedelta(minutes=1), T0)


def test_fault_신규_거부와_취소_거부(tmp_path):
    class Reject(FaultPolicy):
        def __init__(self):
            self.reject_new = True

        def reject_order(self, symbol):
            return "주문 거부(시나리오)" if self.reject_new else None

        def reject_cancel(self, order_no):
            return "취소 거부(시나리오)"

    faults = Reject()
    env = Env(tmp_path, faults=faults)
    assert not env.engine.submit("005930", "buy", "market", 1).ok  # 신규 거부
    faults.reject_new = False
    pending = env.engine.submit("005930", "buy", "limit", 10,
                                limit_price=99_000)
    assert pending.ok
    result = env.engine.cancel(pending.order_no)  # 실존 미체결 주문 취소 거부
    assert not result.ok and "취소 거부" in result.reason
    # 존재 확인이 결함 훅보다 먼저(트레이더 Minor — 시나리오 로그 구분)
    ghost = env.engine.cancel("R9999999")
    assert not ghost.ok and ghost.reason == "order not open"


def test_억제_해제된_마켓터블_지정가는_현재가로_체결(tmp_path):
    """트레이더 R3 #2 — 접수 시점 마켓터블이었으나 체결 억제된 지정가가
    해제 후 재개되면 limit(100,500)가 아니라 **재개 시점 현재가(98,500)**로
    체결(§8 마켓터블 재평가 — 스탠딩 지정가의 실서버 관례)."""
    class Suppress(FaultPolicy):
        def __init__(self):
            self.on = True

        def suppress_fill(self, order_no):
            return self.on

    faults = Suppress()
    env = Env(tmp_path, faults=faults)
    result = env.engine.submit("005930", "buy", "limit", 10,
                               limit_price=100_500)  # 마켓터블이지만 억제
    assert result.ok
    assert env.account.open_orders[result.order_no].unfilled == 10
    env.replay_ts = T0 + timedelta(minutes=1, seconds=30)  # 현재가 98,500
    faults.on = False
    env.engine.check_fills()
    assert env.account.holdings["005930"].avg_price == 98_500  # limit 아님
