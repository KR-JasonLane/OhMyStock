"""분석 도메인의 외부 계약. 이 모듈은 특정 LLM/뉴스 벤더를 알지 못한다."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class LlmError(Exception):
    """LLM 호출 실패(접속 불가/타임아웃/HTTP 오류). 런 실패로 이어진다(스펙 §8)."""


class NewsError(Exception):
    """뉴스 조회 실패. 경고 후 뉴스 없이 진행한다(스펙 §4)."""


@dataclass(frozen=True)
class Headline:
    title: str          # 태그 제거된 제목
    url: str            # 원본 기사 링크(제공 시) 우선, 없으면 포털 링크
    published_at: str   # 원문 pubDate 문자열 보존


@dataclass(frozen=True)
class StrategyDetailInput:
    strategy: str
    signal: bool
    avg_return: float
    win_rate: float
    occurrences: int


@dataclass(frozen=True)
class CandidateInput:
    symbol: str
    name: str
    sector_name: str
    total_score: float
    sector_score: float
    strategy_score_norm: float
    details: tuple[StrategyDetailInput, ...]


@dataclass(frozen=True)
class MarketSnapshot:
    """economist 입력용 시장 집계 — 스펙 §5-1.

    sector_table: 42개 산업 업종의 'name r5 r20 r60' 정렬 텍스트 표.
    breadth: R20 > 0인 산업 업종 비율(0~1)."""
    sector_table: str
    breadth: float


@runtime_checkable
class LlmPort(Protocol):
    async def generate_json(self, system: str, prompt: str) -> str:
        """JSON 모드로 생성한 원문 텍스트를 반환. 실패는 LlmError."""
        ...


@runtime_checkable
class NewsPort(Protocol):
    async def search_headlines(self, query: str, limit: int) -> list[Headline]:
        """최신순 헤드라인 검색. 실패는 NewsError."""
        ...
