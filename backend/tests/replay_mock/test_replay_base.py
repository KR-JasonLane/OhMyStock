"""리플레이 R2(시계/데이터/계좌/틱 복제) — 스펙 §4/§5/§6/§7 계약."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from replay.account import Account, OpenOrder
from replay.clock import KST, ReplayClock
from replay.minute_store import MinuteStore
from replay.ticks import EQUITY_TICKS, ETF_TICK, is_on_tick, tick_size

ANCHOR = datetime(2026, 7, 10, 9, 0, tzinfo=KST)


# ── 임포트 격리(스펙 §4 — 관습이 아니라 테스트가 강제) ──────────────────

def test_replay는_app을_임포트하지_않는다():
    """AST 기반(아키텍트 R2 #3 — 문자열 startswith는 공백 변형·동적 임포트를
    놓친다): Import/ImportFrom 노드의 모듈명이 app/app.*인지 판정 +
    importlib/__import__ 호출의 문자열 인자도 검사."""
    import ast
    replay_dir = Path(__file__).resolve().parents[2] / "replay"
    offenders = []
    for path in replay_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app" or alias.name.startswith("app."):
                        offenders.append(f"{path.name}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level == 0 and (module == "app"
                                        or module.startswith("app.")):
                    offenders.append(f"{path.name}:{node.lineno}")
            elif isinstance(node, ast.Call):
                # importlib.import_module("app...") / __import__("app...")
                func = node.func
                name = getattr(func, "attr", getattr(func, "id", ""))
                if name in ("import_module", "__import__") and node.args:
                    arg = node.args[0]
                    if (isinstance(arg, ast.Constant)
                            and isinstance(arg.value, str)
                            and (arg.value == "app"
                                 or arg.value.startswith("app."))):
                        offenders.append(f"{path.name}:{node.lineno}(dynamic)")
    assert offenders == [], (
        "replay/는 app을 임포트하면 안 된다(자기참조 검증 무력화 — 스펙 §4): "
        f"{offenders}")


def test_replay_임포트가_app을_런타임에_끌어들이지_않는다():
    """동적 임포트 우회의 런타임 감사(아키텍트 R2 #3) — replay 모듈군 임포트
    후 sys.modules에 app 계열이 새로 등장하면 실패."""
    import importlib
    import sys
    before = {name for name in sys.modules if name.split(".")[0] == "app"}
    for module in ("replay", "replay.clock", "replay.minute_store",
                   "replay.account", "replay.ticks"):
        importlib.import_module(module)
    after = {name for name in sys.modules if name.split(".")[0] == "app"}
    assert after == before, f"replay 임포트가 app을 로드함: {after - before}"


# ── 틱 복제 값 대조(스펙 §4 — import가 아니라 값 비교) ──────────────────

def test_틱_테이블은_app과_값이_일치한다():
    from app.domain.trading import ticks as app_ticks
    assert EQUITY_TICKS == app_ticks._EQUITY_TICKS
    assert ETF_TICK == app_ticks._ETF_TICK
    # 대표 가격대 함수 동작 대조
    for price in (1_500, 3_000, 7_000, 30_000, 100_000, 244_750, 530_000):
        assert tick_size(price, "kospi") == app_ticks.tick_size(price, "kospi")
    assert is_on_tick(244_750, "kospi") is False  # RC4003 실측 사례
    assert is_on_tick(244_500, "kospi") is True
    # 에러 경로도 대조(개발자 R2 #2 — 정상값만 대조하면 검증 분기 드리프트를
    # 못 잡는다): 미지 market은 양쪽 모두 ValueError
    with pytest.raises(ValueError):
        tick_size(100_000, "weird_market")
    with pytest.raises(ValueError):
        app_ticks.tick_size(100_000, "weird_market")


# ── 시계(§5) ────────────────────────────────────────────────────────────

def test_clock은_앵커에서_실경과만큼_진행한다():
    fake_time = [100.0]
    clock = ReplayClock(ANCHOR, monotonic=lambda: fake_time[0])
    assert clock.now() == ANCHOR
    fake_time[0] += 90  # 실 90초 경과
    assert clock.now() == ANCHOR + timedelta(seconds=90)
    assert clock.now().tzinfo is not None  # KST-aware 계약(§4-1)


def test_clock_배속은_경과를_스케일한다():
    fake_time = [0.0]
    clock = ReplayClock(ANCHOR, speed=10.0, monotonic=lambda: fake_time[0])
    fake_time[0] += 60
    assert clock.now() == ANCHOR + timedelta(minutes=10)
    assert clock.speed == 10.0  # 스탬프 노출(§5 구조적 강제)


def test_clock은_naive_앵커를_KST로_간주하고_UTC는_변환한다():
    naive = ReplayClock(datetime(2026, 7, 10, 9, 0),
                        monotonic=lambda: 0.0)
    assert naive.anchor == ANCHOR
    utc = ReplayClock(datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc),
                      monotonic=lambda: 0.0)
    assert utc.anchor == ANCHOR  # UTC 00:00 == KST 09:00


# ── 데이터 로더(§6) ─────────────────────────────────────────────────────

def _make_db(tmp_path: Path) -> Path:
    """실측 형태(부호 프리픽스·cntr_tm YYYYMMDDHHMMSS) 그대로의 픽스처."""
    db = tmp_path / "minutes.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE minute_raw(symbol TEXT, page INTEGER, "
                 "seq INTEGER, row TEXT, PRIMARY KEY(symbol, seq))")
    rows = [
        # 내림차순 저장(실측 — 수집 원문 순서) → 로더가 재정렬해야 함
        ("005930", 0, 0, {"cur_prc": "+100500", "trde_qty": "10",
                          "cntr_tm": "20260710090200", "open_pric": "+100300",
                          "high_pric": "+100600", "low_pric": "-100200",
                          "acc_trde_qty": "30", "pred_pre": "+500",
                          "pred_pre_sig": "2"}),
        ("005930", 0, 1, {"cur_prc": "-100000", "trde_qty": "20",
                          "cntr_tm": "20260710090000", "open_pric": "+100100",
                          "high_pric": "+100200", "low_pric": "-99900",
                          "acc_trde_qty": "20", "pred_pre": "-100",
                          "pred_pre_sig": "5"}),
        # degenerate(012510 실측 재현 — 전 필드 빈 문자열)
        ("012510", 0, 0, {"cur_prc": "", "trde_qty": "", "cntr_tm": "",
                          "open_pric": "", "high_pric": "", "low_pric": "",
                          "acc_trde_qty": "", "pred_pre": "",
                          "pred_pre_sig": ""}),
    ]
    for symbol, page, seq, row in rows:
        conn.execute("INSERT INTO minute_raw VALUES (?,?,?,?)",
                     (symbol, page, seq, json.dumps(row)))
    conn.commit()
    conn.close()
    return db


def test_로더는_실측_형태를_파싱하고_정렬한다(tmp_path):
    store = MinuteStore.load(_make_db(tmp_path))
    assert store.symbols == ["005930"]  # degenerate 심볼은 행 스킵
    assert store.skipped == 1           # 침묵 금지 — 스킵 카운트 노출
    first, last = store.span("005930")
    assert first == datetime(2026, 7, 10, 9, 0, tzinfo=KST)   # 오름차순 재정렬
    assert last == datetime(2026, 7, 10, 9, 2, tzinfo=KST)
    candle = store.candle_at("005930", datetime(2026, 7, 10, 9, 0, 30,
                                                tzinfo=KST))
    assert candle.close == 100_000 and candle.low == 99_900  # 부호 제거


def test_로더는_ts_이후_데이터를_주지_않는다(tmp_path):
    # §5 제1 불변식 — "이후" 접근 API 자체가 없다: at/last_at_or_before만 검증
    store = MinuteStore.load(_make_db(tmp_path))
    ts = datetime(2026, 7, 10, 9, 1, tzinfo=KST)  # 09:01(결측 분)
    assert store.candle_at("005930", ts) is None          # 정확 분 결측
    held = store.last_at_or_before("005930", ts)
    assert held.ts == datetime(2026, 7, 10, 9, 0, tzinfo=KST)  # 직전 유지
    before_open = datetime(2026, 7, 10, 8, 59, tzinfo=KST)
    assert store.last_at_or_before("005930", before_open) is None


def test_로더_naive_조회시각은_KST로_간주(tmp_path):
    """개발자 R2 #1 — astimezone 단독은 naive를 시스템 tz로 간주(market_
    calendar 실버그 클래스). naive는 KST 벽시계로 해석돼야 한다."""
    store = MinuteStore.load(_make_db(tmp_path))
    naive = datetime(2026, 7, 10, 9, 0, 30)  # tz 없음 — KST로 간주
    candle = store.candle_at("005930", naive)
    assert candle is not None and candle.close == 100_000


def test_로더_중복_ts는_나중_seq가_이긴다(tmp_path):
    """페이지 경계 중복 수집(같은 분봉이 두 페이지에) 정책 고정(개발자 R2 #4)."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO minute_raw VALUES ('005930', 1, 2, ?)",
        (json.dumps({"cur_prc": "+100999", "trde_qty": "5",
                     "cntr_tm": "20260710090000", "open_pric": "+100100",
                     "high_pric": "+100200", "low_pric": "-99900",
                     "acc_trde_qty": "25", "pred_pre": "+899",
                     "pred_pre_sig": "2"}),))
    conn.commit()
    conn.close()
    store = MinuteStore.load(db)
    candle = store.candle_at("005930", datetime(2026, 7, 10, 9, 0, tzinfo=KST))
    assert candle.close == 100_999  # 나중 seq(재조회분) 채택


def test_로더_첫_행이_이형이어도_전체가_죽지_않는다(tmp_path):
    """개발자 R2 #3 — 필드셋 감지가 첫 행 표본에 갇히면 '행 하나 이상'과
    '전체 드리프트'가 구분 안 된다. 인식되는 행이 나올 때까지 스킵."""
    db = tmp_path / "mixed.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE minute_raw(symbol TEXT, page INTEGER, "
                 "seq INTEGER, row TEXT, PRIMARY KEY(symbol, seq))")
    conn.execute("INSERT INTO minute_raw VALUES ('005930', 0, 0, ?)",
                 (json.dumps({"alien": "1"}),))  # 키 자체가 다른 이형 첫 행
    conn.execute("INSERT INTO minute_raw VALUES ('005930', 0, 1, ?)",
                 (json.dumps({"cur_prc": "+100000", "trde_qty": "1",
                              "cntr_tm": "20260710090000",
                              "open_pric": "+100000", "high_pric": "+100000",
                              "low_pric": "+100000", "acc_trde_qty": "1",
                              "pred_pre": "0", "pred_pre_sig": "3"}),))
    conn.commit()
    conn.close()
    store = MinuteStore.load(db)
    assert store.symbols == ["005930"] and store.skipped == 1


def test_로더_symbols_since_필터(tmp_path):
    # 아키텍트 R2 #2 — 재생 서버는 앵커 이후·대상 심볼만 적재(842MB 방지)
    store = MinuteStore.load(_make_db(tmp_path), symbols=["005930"],
                             since=datetime(2026, 7, 10, 9, 1, tzinfo=KST))
    first, last = store.span("005930")
    assert first == datetime(2026, 7, 10, 9, 2, tzinfo=KST)  # 09:00 제외


def test_로더는_전부_실패하면_fail_loud(tmp_path):
    db = tmp_path / "bad.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE minute_raw(symbol TEXT, page INTEGER, "
                 "seq INTEGER, row TEXT)")
    conn.execute("INSERT INTO minute_raw VALUES ('X', 0, 0, ?)",
                 (json.dumps({"alien_field": "1"}),))
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="unrecognized|drift"):
        MinuteStore.load(db)


# ── 계좌(§7 — 모의 수수료 실측률) ───────────────────────────────────────

def test_계좌_매수_매도_수수료와_세금():
    acct = Account(cash=10_000_000)
    acct.apply_buy_fill("005930", "kospi", 10, 100_000)
    # 매수: 1,000,000 + 수수료 0.35% = 3,500
    assert acct.cash == 10_000_000 - 1_000_000 - 3_500
    assert acct.holdings["005930"].avg_price == 100_000
    acct.apply_sell_fill("005930", 10, 110_000)
    # 매도: 1,100,000 − 수수료 3,850 − 세금 0.2% 2,200
    assert acct.cash == (10_000_000 - 1_000_000 - 3_500
                         + 1_100_000 - 3_850 - 2_200)
    assert "005930" not in acct.holdings


def test_계좌_ETF는_거래세_면제():
    acct = Account(cash=1_000_000)
    acct.apply_buy_fill("069500", "etf", 10, 40_000)
    acct.apply_sell_fill("069500", 10, 40_000)
    # 세금 0 — 수수료만 왕복(1,400+1,400)
    assert acct.cash == 1_000_000 - 1_400 - 1_400


def test_계좌_초과_매도는_fail_loud():
    acct = Account(cash=1_000_000)
    acct.apply_buy_fill("005930", "kospi", 5, 100_000)
    with pytest.raises(ValueError, match="oversell"):
        acct.apply_sell_fill("005930", 6, 100_000)


def test_계좌_전파_지연_창은_미체결을_숨긴다():
    from replay.account import OpenOrder
    acct = Account(cash=0)
    t0 = datetime(2026, 7, 22, 9, 0, tzinfo=KST)
    acct.open_orders["R1"] = OpenOrder(
        order_no="R1", symbol="005930", side="buy", style="limit",
        quantity=10, unfilled=10, price=100_000, submitted_at=t0,
        visible_after=t0 + timedelta(seconds=2))
    assert acct.visible_open_orders(t0) == []                    # 전파 전(C1)
    assert len(acct.visible_open_orders(
        t0 + timedelta(seconds=2))) == 1                         # 전파 후

def test_계좌_평단_절삭_드리프트는_가시화된다(caplog):
    """트레이더 R2 #3 회귀 — 정수 평단(//) 반복 분할매도의 잔여 원가가
    전량 청산 시 침묵 폐기되지 않고 경고+누적 카운터로 표면화된다
    (reconcile 검증 판정 기준 오염 방지)."""
    import logging
    acct = Account(cash=10_000_000)
    # 분할 매수로 총원가 203(비가분) — 평단 절삭 101, 2주 전량 매도 시
    # 차감 202로 잔여 1원(절삭 드리프트의 최소 재현)
    acct.apply_buy_fill("005930", "kospi", 1, 100)
    acct.apply_buy_fill("005930", "kospi", 1, 103)
    with caplog.at_level(logging.WARNING, logger="replay.account"):
        acct.apply_sell_fill("005930", 2, 100)  # 전량 청산 — 잔여 1원
    assert acct.cost_drift_total == 1
    assert any("drift" in r.message for r in caplog.records)
    assert "005930" not in acct.holdings  # 청산 자체는 정상 완결


def test_계좌_평가_합계는_kt00018_최상위_필드_원천():
    acct = Account(cash=0)
    acct.apply_buy_fill("005930", "kospi", 10, 100_000)
    total_eval, total_profit = acct.eval_total({"005930": 103_000})
    assert total_eval == 1_030_000 and total_profit == 30_000
