from app.domain.analysis.config import AnalysisConfig
from app.domain.analysis.parsing import MarketContext
from app.domain.analysis.ports import (CandidateInput, Headline,
                                       MarketSnapshot, StrategyDetailInput)
from app.domain.analysis.prompts import (ECONOMIST_SYSTEM, TRADER_SYSTEM,
                                         build_economist_prompt,
                                         build_trader_prompt, prompt_hash)

CAND = CandidateInput(
    symbol="005930", name="삼성전자", sector_name="전기/전자",
    total_score=0.9, sector_score=1.0, strategy_score_norm=0.8,
    details=(StrategyDetailInput("momentum", True, 0.05, 0.6, 3),))
CTX = MarketContext("neutral", "요약", 5, ("금리",))


def test_시스템_프롬프트_필수_요소():
    for system in (ECONOMIST_SYSTEM, TRADER_SYSTEM):
        assert "JSON" in system
        assert "만들어내지" in system          # 환각 억제
        assert "<뉴스>" in system              # 인젝션 완화 구획 지시
    assert "neutral" in ECONOMIST_SYSTEM
    assert "reject" in TRADER_SYSTEM
    assert "얇은" in TRADER_SYSTEM             # 표본 경고


def test_economist_프롬프트_구성():
    snap = MarketSnapshot(sector_table="화학 0.01 0.02 0.03", breadth=0.4)
    p = build_economist_prompt(snap, [Headline("코스피 하락", "u", "d")])
    assert "화학" in p and "40" in p            # 시장 폭 % 표기
    assert "<뉴스>" in p and "코스피 하락" in p


def test_trader_프롬프트_구성_뉴스없음():
    p = build_trader_prompt(CAND, CTX, [])
    assert "삼성전자" in p and "momentum" in p and "3" in p  # 발생 횟수 노출
    assert "<뉴스>없음</뉴스>" in p


def test_prompt_hash_결정론():
    assert prompt_hash() == prompt_hash()
    assert len(prompt_hash()) == 12
