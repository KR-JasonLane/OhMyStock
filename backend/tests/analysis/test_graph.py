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


@pytest.mark.anyio
async def test_economist_폴백과_trader_성공이_공존한다():
    """LangGraph 상태 채널 병합(LastValue) 회귀 테스트 — economist가 폴백
    경고를 남긴 뒤에도 이어지는 trader 노드의 승인 결과(verdicts/picks)가
    유실 없이 최종 상태까지 전달돼야 한다."""
    llm = ScriptedLlm(["broken", "also broken", approve(0.9)])  # economist 2회 실패 → 폴백
    result = await AnalysisPipeline(llm, CFG).run(SNAP, [cand("AAA111", 0.9)], [], {})
    assert "economist-parse-fallback" in result.warnings
    assert result.verdicts["AAA111"].verdict == "approve"
    assert result.picks == (Pick("AAA111", 1),)


@pytest.mark.parametrize("var", ["LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2",
                                 "LANGSMITH_TRACING", "LANGCHAIN_TRACING"])
def test_langsmith_추적_환경변수_활성화시_초기화가_거부된다(monkeypatch, var):
    """설치된 langsmith SDK가 실제 인식하는 4개 이름 전부 차단 (보안 패널 실측 —
    2개만 덮으면 LANGSMITH_TRACING_V2 등 우선순위 높은 이름으로 우회 가능)."""
    for v in ("LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2",
              "LANGSMITH_TRACING", "LANGCHAIN_TRACING"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv(var, "true")
    with pytest.raises(RuntimeError):
        AnalysisPipeline(ScriptedLlm([]), CFG)


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


@pytest.mark.anyio
async def test_주입된_cfg의_round_trip_cost_pct가_trader_프롬프트에_반영된다():
    """T2 패널 4인 공통 지적 회귀 테스트 — 과거에는 traders 노드가 모듈 상수
    TRADER_SYSTEM(기본 AnalysisConfig()로 import 시점에 한 번 렌더링됨)을
    그대로 썼기 때문에, 주입된 cfg.round_trip_cost_pct가 실제 LLM 프롬프트에
    반영되지 않았다 — analysis_runs.config에는 주입값이 스냅샷되는데 실제
    LLM 입력은 기본값(0.25%p)인 채로 어긋나 감사(audit) 짝이 깨졌다."""
    cfg = AnalysisConfig(parse_retries=1, round_trip_cost_pct=0.5)
    llm = ScriptedLlm([ECON_OK, approve(0.9)])
    await AnalysisPipeline(llm, cfg).run(SNAP, [cand("AAA111", 0.9)], [], {})
    trader_system, _ = llm.calls[1]  # economist가 첫 호출, trader가 두 번째
    assert "0.5" in trader_system
    assert "0.25" not in trader_system
