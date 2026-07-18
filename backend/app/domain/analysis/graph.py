"""분석 파이프라인 — LangGraph로 economist → traders 노드를 순차 실행한다.

경제 국면 판정(economist)과 종목별 매매 판정(trader)은 서로 다른 LLM 호출
단계이므로 별도 그래프 노드로 분리한다. `synthesize`는 승인된 판정을 실제
매수 후보(Pick)로 좁히는 후처리 규칙으로, LLM을 호출하지 않는 순수 함수라
그래프 밖에 둔다 (스펙 §5-3 — 선정 규칙은 결정론적이어야 재현·테스트 가능).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.domain.analysis.config import AnalysisConfig
from app.domain.analysis.parsing import (MarketContext, ParseError,
                                         TraderVerdict, neutral_fallback,
                                         parse_failure_reject,
                                         parse_market_context,
                                         parse_trader_verdict)
from app.domain.analysis.ports import (CandidateInput, Headline, LlmPort,
                                       MarketSnapshot)
from app.domain.analysis.prompts import (ECONOMIST_SYSTEM, TRADER_SYSTEM,
                                         build_economist_prompt,
                                         build_trader_prompt)


@dataclass(frozen=True)
class Pick:
    symbol: str
    rank: int


@dataclass(frozen=True)
class AnalysisResult:
    market: MarketContext
    verdicts: dict[str, TraderVerdict]
    picks: tuple[Pick, ...]
    warnings: tuple[str, ...]


class _State(TypedDict):
    market: MarketContext | None
    verdicts: dict[str, TraderVerdict]
    warnings: list[str]


def synthesize(market: MarketContext, verdicts: dict[str, TraderVerdict],
               candidates: Sequence[CandidateInput],
               cfg: AnalysisConfig) -> tuple[Pick, ...]:
    """approve 판정만 골라 신뢰도*종합점수 내림차순(동률은 종목코드순)으로
    정렬하고, min(cfg.max_picks, market.max_picks_advice)로 자른다.

    risk_off 국면이라도 economist가 advice를 0보다 크게 제시했다면 그 수만큼
    선정한다 — economist의 수치 판단을 신뢰하고, 국면 라벨만으로 별도
    페널티를 적용하지 않는다 (판정 로직 이중화 방지)."""
    cap = min(cfg.max_picks, market.max_picks_advice)
    if cap <= 0:
        return ()
    candidates_by_symbol = {c.symbol: c for c in candidates}
    approved = []
    for symbol, verdict in verdicts.items():
        if verdict.verdict != "approve":
            continue
        candidate = candidates_by_symbol.get(symbol)
        if candidate is None:
            continue
        sort_key = (-verdict.confidence * candidate.total_score, symbol)
        approved.append((sort_key, symbol))
    approved.sort(key=lambda item: item[0])
    return tuple(Pick(symbol=symbol, rank=rank)
                 for rank, (_, symbol) in enumerate(approved[:cap], start=1))


class AnalysisPipeline:
    """`run()`마다 달라지는 입력(스냅샷·후보·뉴스)은 인스턴스 필드로 주입한
    뒤 컴파일된 그래프를 실행한다 — 그래프 자체는 `__init__`에서 한 번만
    구성한다."""

    def __init__(self, llm: LlmPort, cfg: AnalysisConfig) -> None:
        self._llm = llm
        self._cfg = cfg
        self._snapshot: MarketSnapshot | None = None
        self._candidates: Sequence[CandidateInput] = ()
        self._market_headlines: Sequence[Headline] = ()
        self._symbol_headlines: dict[str, list[Headline]] = {}

        graph = StateGraph(_State)
        graph.add_node("economist", self._economist_node)
        graph.add_node("traders", self._traders_node)
        graph.add_edge(START, "economist")
        graph.add_edge("economist", "traders")
        graph.add_edge("traders", END)
        self._graph = graph.compile()

    async def _economist_node(self, state: _State) -> dict:
        prompt = build_economist_prompt(self._snapshot, self._market_headlines)
        for _ in range(self._cfg.parse_retries + 1):
            try:
                raw = await self._llm.generate_json(ECONOMIST_SYSTEM, prompt)
                return {"market": parse_market_context(raw, self._cfg.max_picks)}
            except ParseError:
                continue
        return {
            "market": neutral_fallback(self._cfg.max_picks),
            "warnings": [*state["warnings"], "economist-parse-fallback"],
        }

    async def _traders_node(self, state: _State) -> dict:
        market = state["market"]
        verdicts = dict(state["verdicts"])
        warnings = list(state["warnings"])
        for candidate in self._candidates:
            headlines = self._symbol_headlines.get(candidate.symbol, [])
            prompt = build_trader_prompt(candidate, market, headlines)
            verdict: TraderVerdict | None = None
            for _ in range(self._cfg.parse_retries + 1):
                try:
                    raw = await self._llm.generate_json(TRADER_SYSTEM, prompt)
                    verdict = parse_trader_verdict(raw)
                    break
                except ParseError:
                    continue
            if verdict is None:
                verdict = parse_failure_reject()
                warnings.append(f"trader-parse-failure:{candidate.symbol}")
            verdicts[candidate.symbol] = verdict
        return {"verdicts": verdicts, "warnings": warnings}

    async def run(self, snapshot: MarketSnapshot,
                  candidates: Sequence[CandidateInput],
                  market_headlines: Sequence[Headline],
                  symbol_headlines: dict[str, list[Headline]]) -> AnalysisResult:
        self._snapshot = snapshot
        self._candidates = candidates
        self._market_headlines = market_headlines
        self._symbol_headlines = symbol_headlines

        final_state: _State = await self._graph.ainvoke(
            {"market": None, "verdicts": {}, "warnings": []})

        market = final_state["market"]
        verdicts = final_state["verdicts"]
        picks = synthesize(market, verdicts, candidates, self._cfg)
        return AnalysisResult(market=market, verdicts=verdicts, picks=picks,
                              warnings=tuple(final_state["warnings"]))
