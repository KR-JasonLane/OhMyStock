"""분석 파라미터 단일 출처. 스펙 §5-5와 1:1 — 값 변경은 스펙 갱신과 함께.
실행마다 스냅샷(JSON)이 analysis_runs.config에 기록된다 (재현성)."""

import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class AnalysisConfig:
    model: str = "exaone3.5:7.8b"
    temperature: float = 0.2
    max_picks: int = 5
    news_per_symbol: int = 5
    market_keywords: tuple[str, ...] = ("코스피", "코스닥", "증시")
    parse_retries: int = 2
    llm_timeout_s: float = 120.0
    score_max_age_days: int = 3
    ollama_base_url: str = "http://host.docker.internal:11434"

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
