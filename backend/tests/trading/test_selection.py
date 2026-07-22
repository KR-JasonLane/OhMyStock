"""select_entries — 필터 체인·고정 슬롯 사이징 경계 검증(스펙 §6-3)."""

import pytest

from app.domain.trading.config import TradingConfig
from app.domain.trading.selection import EntryCandidate, select_entries

CFG = TradingConfig(max_single_order_krw=10_000_000, max_daily_orders=50,
                    max_daily_order_krw=50_000_000,
                    min_avg_trading_value_krw=1_000_000_000,
                    commission_buy_pct=0.35)


def cand(symbol="005930", *, signal=100_000, current=100_000,
         audit="정상", state="증거금100%", liquidity=5_000_000_000,
         market="kospi") -> EntryCandidate:
    return EntryCandidate(symbol=symbol, name=symbol, market=market,
                          signal_price=signal, current_price=current,
                          audit_info=audit, state=state,
                          avg_trading_value_krw=liquidity)


def test_고정_슬롯_사이징_분모는_max_positions():
    # 가용 1,000만 × 50% ÷ 5슬롯 = 슬롯당 100만. 후보가 1개여도 100만만 배정
    # (트레이더 v1 Critical — 후보 수로 나누면 결정 #30 분산 붕괴)
    plans = select_entries([cand()], held_symbols=set(),
                           available_krw=10_000_000, config=CFG)
    assert len(plans) == 1
    assert plans[0].budget_krw == 1_000_000
    # 수량 = 100만 × (1−0.0035) ÷ 100,000 = 9.965 → 내림 9주(수수료 버퍼)
    assert plans[0].quantity == 9


def test_잔여_슬롯만큼만_선정하고_순서_유지():
    cands = [cand(f"A{i}") for i in range(5)]
    plans = select_entries(cands, held_symbols={"H1", "H2", "H3"},
                           available_krw=100_000_000, config=CFG)
    assert len(plans) == 2  # 5슬롯 − 보유 3 = 잔여 2
    assert [p.symbol for p in plans] == ["A0", "A1"]  # pick rank 유지


def test_슬롯_가득이면_빈_리스트():
    assert select_entries([cand()], held_symbols={"A", "B", "C", "D", "E"},
                          available_krw=100_000_000, config=CFG) == []


def test_갭_가드_양방향():
    # 기본 3%: 상방 +3.1% 제외, 하방 −3.1%도 제외(악재 갭 — 신호 전제 훼손)
    up = cand("UP", signal=100_000, current=103_100)
    down = cand("DN", signal=100_000, current=96_900)
    ok = cand("OK", signal=100_000, current=102_900)
    plans = select_entries([up, down, ok], set(), 100_000_000, CFG)
    assert [p.symbol for p in plans] == ["OK"]


def test_갭_가드_정확_경계는_통과():
    """정확히 ±3.00%는 포함(통과) — float 나눗셈이면 3.0000000000000027>3.0으로
    오제외되던 부동소수점 경계 버그의 회귀 고정(개발자 Critical, 정수 bp 수정)."""
    edge_up = cand("EU", signal=100_000, current=103_000)    # 정확히 +3.00%
    edge_dn = cand("ED", signal=100_000, current=97_000)     # 정확히 −3.00%
    over = cand("OV", signal=100_000, current=103_001)       # +3.001% — 제외
    under = cand("UN", signal=100_000, current=102_999)      # +2.999% — 통과
    plans = select_entries([edge_up, edge_dn, over, under],
                           set(), 100_000_000, CFG)
    assert [p.symbol for p in plans] == ["EU", "ED", "UN"]


def test_유니버스_필터_거래정지_관리종목_제외():
    halted = cand("HALT", state="증거금100%|거래정지")
    admin = cand("ADM", audit="관리종목")
    plans = select_entries([halted, admin, cand("OK")], set(), 100_000_000, CFG)
    assert [p.symbol for p in plans] == ["OK"]


def test_유동성_필터():
    thin = cand("THIN", liquidity=500_000_000)  # 임계 10억 미만
    plans = select_entries([thin, cand("OK")], set(), 100_000_000, CFG)
    assert [p.symbol for p in plans] == ["OK"]


def test_보유_중복_제외():
    plans = select_entries([cand("HELD"), cand("NEW")], held_symbols={"HELD"},
                           available_krw=100_000_000, config=CFG)
    assert [p.symbol for p in plans] == ["NEW"]


def test_고가주_0주면_스킵():
    # 슬롯 100만으로 200만짜리 1주도 못 삼 → 스킵하고 다음 후보
    pricey = cand("BIG", signal=2_000_000, current=2_000_000)
    plans = select_entries([pricey, cand("OK")], set(), 10_000_000, CFG)
    assert [p.symbol for p in plans] == ["OK"]


def test_가격_결측_후보는_제외():
    plans = select_entries([cand("BAD", current=0), cand("OK")],
                           set(), 100_000_000, CFG)
    assert [p.symbol for p in plans] == ["OK"]


def test_가용자금_0이면_빈_리스트_음수는_에러():
    assert select_entries([cand()], set(), 0, CFG) == []
    with pytest.raises(ValueError, match="available_krw"):
        select_entries([cand()], set(), -1, CFG)


def test_유동성_임계_0은_필터_비활성():
    cfg = TradingConfig(max_single_order_krw=10_000_000, max_daily_orders=50,
                        max_daily_order_krw=50_000_000,
                        min_avg_trading_value_krw=0)
    thin = cand("THIN", liquidity=0)
    assert len(select_entries([thin], set(), 100_000_000, cfg)) == 1
