"""파이프라인·synthesize 결정론 검증 — 스크립트된 가짜 LLM."""

import pytest

from app.domain.analysis.config import AnalysisConfig
from app.domain.analysis.graph import AnalysisPipeline, Pick, synthesize
from app.domain.analysis.parsing import MarketContext, TraderVerdict
from app.domain.analysis.ports import (CandidateInput, LlmError,
                                       MarketSnapshot, StrategyDetailInput)

CFG = AnalysisConfig(parse_retries=1)
SNAP = MarketSnapshot(sector_table="t", breadth=0.5)


def cand(symbol, total):
    return CandidateInput(symbol=symbol, name=symbol, sector_name="s",
                          total_score=total, sector_score=0.5,
                          strategy_score_norm=0.5,
                          details=(StrategyDetailInput("momentum", True,
                                                       0.05, 0.6, 3),))


class ScriptedLlm:
    """호출 순서대로 응답을 소진하는 가짜 — economist가 첫 호출."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def generate_json(self, system, prompt):
        self.calls.append((system, prompt))
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


ECON_OK = '{"regime": "neutral", "summary": "s", "max_picks_advice": 5}'


def approve(conf):
    return f'{{"verdict": "approve", "confidence": {conf}}}'


@pytest.mark.anyio
async def test_파이프라인_정상_경로():
    llm = ScriptedLlm([ECON_OK, approve(0.9), approve(0.5)])
    result = await AnalysisPipeline(llm, CFG).run(
        SNAP, [cand("AAA111", 0.9), cand("BBB222", 0.8)], [], {})
    assert result.market.regime == "neutral"
    assert [p.symbol for p in result.picks] == ["AAA111", "BBB222"]
    assert result.picks[0].rank == 1
    assert result.warnings == ()


@pytest.mark.anyio
async def test_economist_파싱실패는_neutral_폴백():
    llm = ScriptedLlm(["broken", "also broken", approve(0.9)])  # 재시도 1회 포함 2회 실패
    result = await AnalysisPipeline(llm, CFG).run(SNAP, [cand("AAA111", 0.9)], [], {})
    assert result.market.regime == "neutral"
    assert "economist-parse-fallback" in result.warnings
    assert result.picks  # 분석은 계속된다


@pytest.mark.anyio
async def test_trader_파싱실패는_보수_reject():
    llm = ScriptedLlm([ECON_OK, "broken", "broken"])  # trader 2회 모두 실패
    result = await AnalysisPipeline(llm, CFG).run(SNAP, [cand("AAA111", 0.9)], [], {})
    assert result.verdicts["AAA111"].verdict == "reject"
    assert result.verdicts["AAA111"].reasons == ("llm-parse-failure",)
    assert "trader-parse-failure:AAA111" in result.warnings
    assert result.picks == ()


@pytest.mark.anyio
async def test_LlmError는_전파된다():
    llm = ScriptedLlm([LlmError("ollama down")])
    with pytest.raises(LlmError):
        await AnalysisPipeline(llm, CFG).run(SNAP, [cand("AAA111", 0.9)], [], {})


def test_synthesize_정렬과_상한과_동률():
    market = MarketContext("risk_off", "s", 1, ())   # advice=1이 상한
    verdicts = {
        "AAA111": TraderVerdict("approve", 0.8, (), ()),
        "BBB222": TraderVerdict("approve", 0.9, (), ()),
        "CCC333": TraderVerdict("reject", 0.9, (), ()),
    }
    cands = [cand("AAA111", 0.9), cand("BBB222", 0.8), cand("CCC333", 0.99)]
    picks = synthesize(market, verdicts, cands, AnalysisConfig())
    assert picks == (Pick("AAA111", 1),)  # 0.8*0.9=0.72 == 0.9*0.8 동률 → 코드순, 상한 1


def test_synthesize_advice_0이면_빈_리스트():
    market = MarketContext("risk_off", "s", 0, ())
    verdicts = {"AAA111": TraderVerdict("approve", 1.0, (), ())}
    assert synthesize(market, verdicts, [cand("AAA111", 0.9)], AnalysisConfig()) == ()
