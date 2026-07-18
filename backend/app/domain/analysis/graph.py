"""분석 파이프라인 — LangGraph로 economist → traders 노드를 순차 실행한다.

경제 국면 판정(economist)과 종목별 매매 판정(trader)은 서로 다른 LLM 호출
단계이므로 별도 그래프 노드로 분리한다. `synthesize`는 승인된 판정을 실제
매수 후보(Pick)로 좁히는 후처리 규칙으로, LLM을 호출하지 않는 순수 함수라
그래프 밖에 둔다 (스펙 §5-3 — 선정 규칙은 결정론적이어야 재현·테스트 가능).
"""

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypedDict, TypeVar

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

_T = TypeVar("_T")

# LangSmith 텔레메트리 옵트인 환경변수 — 활성화 시 프롬프트/응답(전략 기밀)이
# 외부 SaaS로 전송되므로 파이프라인 구성 시점에 차단한다 (P4-T2 보안 패널).
# 4개 전부 커버해야 한다: 설치된 langsmith(utils.get_env_var, namespaces
# LANGSMITH/LANGCHAIN × TRACING_V2/TRACING)가 실제로 이 4개 이름을 순서대로
# 조회함을 SDK 소스로 실측 확인(보안 패널 재검증). SDK 버전업 시 이름 드리프트
# 가능 — langsmith 업그레이드 시 이 목록을 재확인할 것.
_LANGSMITH_ENV_VARS = ("LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2",
                       "LANGSMITH_TRACING", "LANGCHAIN_TRACING")


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
    # run() 입력 — 그래프 상태 채널로 격리되어 노드 간에만 공유된다
    # (인스턴스 필드가 아니므로 동시 run() 호출 간 상태 공유가 없다).
    snapshot: MarketSnapshot
    candidates: Sequence[CandidateInput]
    market_headlines: Sequence[Headline]
    symbol_headlines: dict[str, list[Headline]]
    # 노드가 채워나가는 결과
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
    """인스턴스는 불변 의존성(`_llm`, `_cfg`, 컴파일된 `_graph`)만 보유한다 —
    run() 동시 호출에도 상태 공유가 없다(입력은 그래프 상태 채널로 격리되며,
    `run()`마다 새 초기 상태를 `ainvoke`에 전달한다)."""

    def __init__(self, llm: LlmPort, cfg: AnalysisConfig) -> None:
        for var in _LANGSMITH_ENV_VARS:
            if os.environ.get(var, "").strip().lower() in ("1", "true", "yes"):
                raise RuntimeError(
                    f"{var} 활성화 금지 - LangSmith 트레이싱은 프롬프트/응답(전략 기밀)을 "
                    "외부 SaaS로 전송한다 (P4-T2 보안 패널)")

        self._llm = llm
        self._cfg = cfg

        graph = StateGraph(_State)
        graph.add_node("economist", self._economist_node)
        graph.add_node("traders", self._traders_node)
        graph.add_edge(START, "economist")
        graph.add_edge("economist", "traders")
        graph.add_edge("traders", END)
        self._graph = graph.compile()

    async def _generate_with_retry(
            self, system: str, prompt: str,
            parse_fn: Callable[[str], _T]) -> _T | None:
        """`parse_retries + 1`회까지 시도, `ParseError`만 잡아 재시도한다.
        전량 실패하면 `None`(호출자가 폴백 적용 + 경고 기록). `LlmError`는
        잡지 않고 그대로 전파된다(스펙 §8 — LLM 접속 실패는 런 실패)."""
        for _ in range(self._cfg.parse_retries + 1):
            try:
                raw = await self._llm.generate_json(system, prompt)
                return parse_fn(raw)
            except ParseError:
                continue
        return None

    async def _economist_node(self, state: _State) -> dict:
        prompt = build_economist_prompt(state["snapshot"],
                                        state["market_headlines"],
                                        self._cfg.max_picks)
        market = await self._generate_with_retry(
            ECONOMIST_SYSTEM, prompt,
            lambda raw: parse_market_context(raw, self._cfg.max_picks))
        if market is None:
            return {
                "market": neutral_fallback(self._cfg.max_picks),
                "warnings": [*state["warnings"], "economist-parse-fallback"],
            }
        return {"market": market}

    async def _traders_node(self, state: _State) -> dict:
        market = state["market"]
        verdicts = dict(state["verdicts"])
        warnings = list(state["warnings"])
        for candidate in state["candidates"]:
            headlines = state["symbol_headlines"].get(candidate.symbol, [])
            prompt = build_trader_prompt(candidate, market, headlines)
            verdict = await self._generate_with_retry(
                TRADER_SYSTEM, prompt, parse_trader_verdict)
            if verdict is None:
                verdict = parse_failure_reject()
                warnings.append(f"trader-parse-failure:{candidate.symbol}")
            verdicts[candidate.symbol] = verdict
        return {"verdicts": verdicts, "warnings": warnings}

    async def run(self, snapshot: MarketSnapshot,
                  candidates: Sequence[CandidateInput],
                  market_headlines: Sequence[Headline],
                  symbol_headlines: dict[str, list[Headline]]) -> AnalysisResult:
        """candidates 순서는 최종 선정에 영향 없음(synthesize가 재정렬)."""
        initial_state: _State = {
            "snapshot": snapshot,
            "candidates": candidates,
            "market_headlines": market_headlines,
            "symbol_headlines": symbol_headlines,
            "market": None,
            "verdicts": {},
            "warnings": [],
        }
        final_state: _State = await self._graph.ainvoke(initial_state)

        market = final_state["market"]
        verdicts = final_state["verdicts"]
        picks = synthesize(market, verdicts, candidates, self._cfg)
        return AnalysisResult(market=market, verdicts=verdicts, picks=picks,
                              warnings=tuple(final_state["warnings"]))
