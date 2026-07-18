"""LLM JSON 응답의 파싱·검증. 범위/enum을 강제하고 위반은 ParseError —
재시도·폴백 정책(economist=neutral, trader=reject)은 호출자(graph) 소관."""

import json
from dataclasses import dataclass

_REGIMES = ("risk_on", "neutral", "risk_off")
_VERDICTS = ("approve", "reject")
_MAX_ITEM_LEN = 200
_MAX_SUMMARY_LEN = 500


class ParseError(ValueError):
    pass


def _short(value) -> str:
    """ParseError 메시지에 넣을 값 절단 — LLM 통제 값이 로그로 흐를 때의
    인젝션/폭주 방지."""
    return repr(value)[:80]


@dataclass(frozen=True)
class MarketContext:
    regime: str
    summary: str
    max_picks_advice: int
    cautions: tuple[str, ...]


@dataclass(frozen=True)
class TraderVerdict:
    verdict: str
    confidence: float
    reasons: tuple[str, ...]
    risk_flags: tuple[str, ...]


def _load_obj(raw: str) -> dict:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ParseError(f"invalid json: {exc}") from exc
    if not isinstance(obj, dict):
        raise ParseError("json root is not an object")
    return obj


def _str_tuple(value, limit: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ParseError("expected a list of strings")
    result = []
    for v in value[:limit]:
        if not isinstance(v, str):
            raise ParseError(f"expected string items: {_short(v)}")
        result.append(v[:_MAX_ITEM_LEN])  # 초과분 절단, 마커 불필요
    return tuple(result)


def parse_market_context(raw: str, max_picks: int) -> MarketContext:
    obj = _load_obj(raw)
    regime = obj.get("regime")
    if regime not in _REGIMES:
        raise ParseError(f"invalid regime: {_short(regime)}")
    advice = obj.get("max_picks_advice")
    if not isinstance(advice, int) or isinstance(advice, bool):
        raise ParseError(f"invalid max_picks_advice: {_short(advice)}")
    # 범위 밖 advice는 오류가 아니라 클램프 — 모델이 6을 말해도 5로 제한(보수 방향)
    advice = max(0, min(advice, max_picks))
    # 텍스트 필드도 타입은 강제, 길이는 절단 — LLM/뉴스 유래 비대 문자열이
    # 저장·표시 계층으로 전파되는 것 차단 (보안 패널)
    raw_summary = obj.get("summary")
    if raw_summary is None:
        summary = ""
    elif not isinstance(raw_summary, str):
        raise ParseError(f"invalid summary: {_short(raw_summary)}")
    else:
        summary = raw_summary[:_MAX_SUMMARY_LEN]
    return MarketContext(regime=regime, summary=summary,
                         max_picks_advice=advice,
                         cautions=_str_tuple(obj.get("cautions"), 5))


def parse_trader_verdict(raw: str) -> TraderVerdict:
    obj = _load_obj(raw)
    verdict = obj.get("verdict")
    if verdict not in _VERDICTS:
        raise ParseError(f"invalid verdict: {_short(verdict)}")
    confidence = obj.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise ParseError(f"invalid confidence: {_short(confidence)}")
    if not 0.0 <= float(confidence) <= 1.0:
        raise ParseError(f"confidence out of range: {_short(confidence)}")
    return TraderVerdict(verdict=verdict, confidence=float(confidence),
                         reasons=_str_tuple(obj.get("reasons"), 3),
                         risk_flags=_str_tuple(obj.get("risk_flags"), 5))


def neutral_fallback(max_picks: int) -> MarketContext:
    """economist 파싱 실패 시 보수 폴백 (스펙 §5-1)."""
    return MarketContext(regime="neutral", summary="economist 응답 파싱 실패 - neutral 폴백",
                         max_picks_advice=max_picks, cautions=())


def parse_failure_reject() -> TraderVerdict:
    """trader 파싱 실패 시 보수 거부 (스펙 §5-2)."""
    return TraderVerdict(verdict="reject", confidence=0.0,
                         reasons=("llm-parse-failure",), risk_flags=())
