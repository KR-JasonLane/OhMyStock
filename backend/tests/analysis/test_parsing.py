"""LLM 응답 파싱·검증 손계산 검증."""

import pytest

from app.domain.analysis.parsing import (MarketContext, ParseError,
                                         TraderVerdict, neutral_fallback,
                                         parse_failure_reject,
                                         parse_market_context,
                                         parse_trader_verdict)


def test_market_context_정상():
    raw = ('{"regime": "risk_off", "summary": "하락 국면", '
           '"max_picks_advice": 2, "cautions": ["금리", "환율"]}')
    ctx = parse_market_context(raw, max_picks=5)
    assert ctx == MarketContext("risk_off", "하락 국면", 2, ("금리", "환율"))


def test_market_context_advice는_클램프된다():
    raw = '{"regime": "risk_on", "max_picks_advice": 99}'
    assert parse_market_context(raw, max_picks=5).max_picks_advice == 5
    raw = '{"regime": "risk_on", "max_picks_advice": -1}'
    assert parse_market_context(raw, max_picks=5).max_picks_advice == 0


@pytest.mark.parametrize("raw", [
    "not json",
    "[1, 2]",
    '{"regime": "bullish", "max_picks_advice": 3}',   # 미지 enum
    '{"regime": "neutral", "max_picks_advice": "3"}',  # 문자열 advice
    '{"regime": "neutral", "max_picks_advice": true}',  # bool 함정
])
def test_market_context_불량은_ParseError(raw):
    with pytest.raises(ParseError):
        parse_market_context(raw, max_picks=5)


def test_trader_verdict_정상():
    raw = ('{"verdict": "approve", "confidence": 0.8, '
           '"reasons": ["a", "b", "c", "d"], "risk_flags": []}')
    v = parse_trader_verdict(raw)
    assert v.verdict == "approve" and v.confidence == 0.8
    assert v.reasons == ("a", "b", "c")  # 3개 초과는 절단


@pytest.mark.parametrize("raw", [
    '{"verdict": "hold", "confidence": 0.5}',        # 미지 enum
    '{"verdict": "approve", "confidence": 1.5}',     # 범위 밖
    '{"verdict": "approve", "confidence": -0.1}',
    '{"verdict": "approve", "confidence": true}',    # bool 함정
    '{"verdict": "approve"}',                        # confidence 부재
])
def test_trader_verdict_불량은_ParseError(raw):
    with pytest.raises(ParseError):
        parse_trader_verdict(raw)


def test_보수_폴백값():
    fb = neutral_fallback(max_picks=5)
    assert fb.regime == "neutral" and fb.max_picks_advice == 5
    rj = parse_failure_reject()
    assert rj.verdict == "reject" and rj.confidence == 0.0
    assert rj.reasons == ("llm-parse-failure",)
