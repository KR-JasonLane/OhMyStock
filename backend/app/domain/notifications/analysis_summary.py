"""아침 AI 분석의 안전한 Telegram plain-text 요약.

이 모듈은 저장소·Telegram adapter·분석 실행 경로에 의존하지 않는다. 읽기
모델이 허용목록 DTO를 만들고, 여기서는 결정론적인 분류와 표시만 수행한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from unicodedata import category

from app.domain.notifications.formatting import render_parts


_ENVIRONMENT_LABEL = {"mock": "모의투자", "real": "🚨 실전"}
_REGIME_LABEL = {
    "risk_on": "위험선호",
    "neutral": "중립",
    "risk_off": "위험회피",
}
_MAX_SYMBOL_LENGTH = 12
_MAX_NAME_LENGTH = 64
_MAX_MARKET_SUMMARY_LENGTH = 500
_MAX_ITEM_LENGTH = 200
_MAX_VERDICTS = 20
_MAX_PICKS = 5
_MAX_REASONS = 3
_MAX_RISK_FLAGS = 5


@dataclass(frozen=True)
class AnalysisVerdictSummary:
    symbol: str
    name: str | None
    verdict: str
    confidence: float
    reasons: tuple[str, ...]
    risk_flags: tuple[str, ...]
    picked: bool
    pick_rank: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str):
            raise ValueError("symbol must contain 1 to 12 characters")
        symbol = _normalize_plain_text(self.symbol)
        if not 0 < len(symbol) <= _MAX_SYMBOL_LENGTH:
            raise ValueError("symbol must contain 1 to 12 characters")
        if self.name is not None and not isinstance(self.name, str):
            raise ValueError("name must be str or None")
        if self.verdict not in {"approve", "reject"}:
            raise ValueError("verdict must be approve or reject")
        if self.picked and self.verdict != "approve":
            raise ValueError("picked verdict must be approve")
        if (not isinstance(self.confidence, (int, float))
                or isinstance(self.confidence, bool)
                or not isfinite(float(self.confidence))
                or not 0.0 <= float(self.confidence) <= 1.0):
            raise ValueError("confidence must be a finite value from 0 to 1")
        if type(self.picked) is not bool:
            raise ValueError("picked must be a bool")
        if self.picked:
            if (not isinstance(self.pick_rank, int)
                    or isinstance(self.pick_rank, bool)
                    or self.pick_rank < 1):
                raise ValueError("pick_rank must be a positive int when picked")
        elif self.pick_rank is not None:
            raise ValueError("pick_rank must be None when not picked")

        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "name", _limited_text(self.name, _MAX_NAME_LENGTH))
        object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(self, "reasons", _limited_items(
            self.reasons, "reasons", _MAX_REASONS))
        object.__setattr__(self, "risk_flags", _limited_items(
            self.risk_flags, "risk_flags", _MAX_RISK_FLAGS))


@dataclass(frozen=True)
class MorningAnalysisSummary:
    run_id: int
    run_environment: str
    regime: str
    market_summary: str
    max_picks_advice: int
    score_reference_date: date
    started_at: datetime
    finished_at: datetime
    verdicts: tuple[AnalysisVerdictSummary, ...]
    corrupted_rows: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, int) or isinstance(self.run_id, bool) or self.run_id < 1:
            raise ValueError("run_id must be a positive int")
        if self.run_environment not in _ENVIRONMENT_LABEL:
            raise ValueError("run_environment must be mock or real")
        if self.regime not in _REGIME_LABEL:
            raise ValueError("regime must be risk_on, neutral, or risk_off")
        if not isinstance(self.market_summary, str):
            raise ValueError("market_summary must be a str")
        if (not isinstance(self.max_picks_advice, int)
                or isinstance(self.max_picks_advice, bool)
                or not 0 <= self.max_picks_advice <= _MAX_PICKS):
            raise ValueError("max_picks_advice must be an int from 0 to 5")
        if type(self.score_reference_date) is not date:
            raise ValueError("score_reference_date must be a date")
        _require_aware(self.started_at, "started_at")
        _require_aware(self.finished_at, "finished_at")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not be before started_at")
        if not isinstance(self.verdicts, tuple) or not all(
                isinstance(verdict, AnalysisVerdictSummary) for verdict in self.verdicts):
            raise ValueError("verdicts must be AnalysisVerdictSummary tuple")
        if len(self.verdicts) > _MAX_VERDICTS:
            raise ValueError("verdicts must contain at most 20 items")
        picks = tuple(verdict for verdict in self.verdicts if verdict.picked)
        if len(picks) > self.max_picks_advice:
            raise ValueError("picked verdicts exceed max_picks_advice")
        if len({verdict.pick_rank for verdict in picks}) != len(picks):
            raise ValueError("picked verdicts must have unique pick_rank")
        if {verdict.pick_rank for verdict in picks} != set(range(1, len(picks) + 1)):
            raise ValueError("picked verdicts must have contiguous pick_rank")
        if (not isinstance(self.corrupted_rows, int)
                or isinstance(self.corrupted_rows, bool)
                or self.corrupted_rows < 0):
            raise ValueError("corrupted_rows must be a non-negative int")

        object.__setattr__(
            self, "market_summary", _limited_text(self.market_summary, _MAX_MARKET_SUMMARY_LENGTH))

    @property
    def idempotency_key(self) -> str:
        return f"analysis-summary:{self.run_environment}:{self.run_id}"


def render_analysis_summary(summary: MorningAnalysisSummary) -> str:
    """분석 결과를 Telegram markup 없이 읽기 쉬운 한국어로 표시한다."""
    final_picks = sorted(
        (verdict for verdict in summary.verdicts if verdict.picked),
        key=lambda verdict: verdict.pick_rank,
    )
    alternate_approvals = sorted(
        (verdict for verdict in summary.verdicts
         if verdict.verdict == "approve" and not verdict.picked),
        key=lambda verdict: (-verdict.confidence, verdict.symbol),
    )
    approved = sum(verdict.verdict == "approve" for verdict in summary.verdicts)
    rejected = sum(verdict.verdict == "reject" for verdict in summary.verdicts)

    lines = [
        f"🧠 아침 AI 분석 완료 · {_ENVIRONMENT_LABEL[summary.run_environment]}",
        "",
        f"시장 국면  {_REGIME_LABEL[summary.regime]}",
        f"최대 진입 권고  {summary.max_picks_advice}종목",
    ]
    if final_picks:
        lines.extend(("", "시장 요약", summary.market_summary or "확인 불가", "", "🎯 최종 후보"))
        for verdict in final_picks:
            lines.append(_display_name(verdict))
            lines.extend(f"- {reason}" for reason in verdict.reasons[:2] or ("확인 불가",))
            lines.extend(("", "주의"))
            lines.extend(
                f"- {risk_flag}" for risk_flag in verdict.risk_flags[:2] or ("확인 불가",))

    else:
        lines.extend(("", "오늘 최종 진입 후보가 없습니다."))

    if alternate_approvals:
        lines.extend(("", "📋 차순위 승인"))
        for verdict in alternate_approvals[:3]:
            lines.extend((
                _display_name(verdict),
                f"- AI는 승인했지만 최대 {summary.max_picks_advice}종목 제한으로 최종 제외",
            ))
        lines.append(f"차순위 승인 전체 {len(alternate_approvals)}종목")

    lines.extend(("", f"검토 결과  승인 {approved} · 거절 {rejected}"))
    if summary.corrupted_rows:
        lines.extend(("", f"확인할 수 없는 분석 행  {summary.corrupted_rows}건"))
    if final_picks:
        lines.extend((
            "",
            "※ AI 분석 결과이며 실제 주문 전 유동성·가격 갭·거래정지·자금 한도",
            "방어선을 다시 확인합니다.",
        ))
    else:
        lines.extend((
            "자동매매는 신규 진입 없이 대기합니다.",
            "",
            "※ AI 분석 결과이며 실제 주문 여부는 거래 방어선이 최종 결정합니다.",
        ))
    return "\n".join(lines)


def render_analysis_parts(summary: MorningAnalysisSummary) -> tuple[str, ...]:
    """기존 plain-text formatter로 Telegram-safe 조각을 만든다."""
    correlation_id = f"analysis-summary-{summary.run_environment}-{summary.run_id}"
    return tuple(part.text for part in render_parts(
        render_analysis_summary(summary), correlation_id))


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _limited_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return _normalize_plain_text(value)[:limit]


def _normalize_plain_text(value: str) -> str:
    normalized = "".join(
        " " if char in "\r\n\u2028\u2029" or category(char) in {"Cc", "Cf", "Cs"} else char
        for char in value)
    return " ".join(normalized.split())


def _limited_items(value: tuple[str, ...], name: str, max_items: int) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a tuple of strings")
    if len(value) > max_items:
        raise ValueError(f"{name} has too many items")
    return tuple(
        normalized for item in value
        if (normalized := _limited_text(item, _MAX_ITEM_LENGTH)))


def _display_name(verdict: AnalysisVerdictSummary) -> str:
    return f"{verdict.symbol} · {verdict.name}" if verdict.name else verdict.symbol
