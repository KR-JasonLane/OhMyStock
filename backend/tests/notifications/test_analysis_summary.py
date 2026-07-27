from datetime import date, datetime, timezone

import pytest

from app.domain.notifications.analysis_summary import (
    AnalysisVerdictSummary,
    MorningAnalysisSummary,
    render_analysis_parts,
    render_analysis_summary,
)


def _verdict(
    symbol: str = "007160",
    name: str | None = "사조산업",
    verdict: str = "approve",
    confidence: float = 0.65,
    picked: bool = True,
    pick_rank: int | None = 1,
    reasons: tuple[str, ...] = ("전략점수가 높음",),
    risk_flags: tuple[str, ...] = ("시장 방향성 불분명",),
) -> AnalysisVerdictSummary:
    return AnalysisVerdictSummary(
        symbol=symbol,
        name=name,
        verdict=verdict,
        confidence=confidence,
        reasons=reasons,
        risk_flags=risk_flags,
        picked=picked,
        pick_rank=pick_rank,
    )


def _summary(**changes: object) -> MorningAnalysisSummary:
    fields: dict[str, object] = {
        "run_id": 42,
        "run_environment": "mock",
        "regime": "neutral",
        "market_summary": "시장의 방향성이 아직 불분명합니다.",
        "max_picks_advice": 1,
        "score_reference_date": date(2026, 7, 27),
        "started_at": datetime(2026, 7, 28, 8, 20, tzinfo=timezone.utc),
        "finished_at": datetime(2026, 7, 28, 8, 21, tzinfo=timezone.utc),
        "verdicts": (_verdict(),),
        "corrupted_rows": 0,
    }
    fields.update(changes)
    return MorningAnalysisSummary(**fields)  # type: ignore[arg-type]


def test_analysis_summary는_최종후보와_차순위승인을_구분한다():
    text = render_analysis_summary(_summary(
        max_picks_advice=1,
        verdicts=(
            _verdict("007160", "사조산업", "approve", 0.65, True, 1),
            _verdict("475150", "SK이터닉스", "approve", 0.65, False, None),
            _verdict("001790", "대한제당", "reject", 0.90, False, None),
        ),
    ))

    assert "🎯 최종 후보" in text
    assert "007160 · 사조산업" in text
    assert "📋 차순위 승인" in text
    assert "475150 · SK이터닉스" in text
    assert "검토 결과  승인 2 · 거절 1" in text


def test_analysis_summary는_점수기준일과_KST완료시각을_명시한다():
    text = render_analysis_summary(_summary(
        score_reference_date=date(2026, 7, 27),
        finished_at=datetime(2026, 7, 28, 8, 21, tzinfo=timezone.utc),
    ))

    assert "점수 기준일  2026-07-27" in text
    assert "분석 완료  2026-07-28 17:21 KST" in text


def test_analysis_summary는_결정론적으로_후보와_차순위를_정렬한다():
    text = render_analysis_summary(_summary(max_picks_advice=2, verdicts=(
        _verdict("007160", "첫째", "approve", 0.10, True, 2),
        _verdict("475150", "둘째", "approve", 0.70, True, 1),
        _verdict("300000", "동점뒤", "approve", 0.50, False, None),
        _verdict("100000", "동점앞", "approve", 0.50, False, None),
        _verdict("900000", "낮음", "approve", 0.40, False, None),
    )))

    assert text.index("475150 · 둘째") < text.index("007160 · 첫째")
    assert text.index("100000 · 동점앞") < text.index("300000 · 동점뒤")


def test_analysis_summary는_후보없음과_자동대기를_표시한다():
    text = render_analysis_summary(_summary(verdicts=(
        _verdict("001790", "대한제당", "reject", 0.90, False, None),
    )))

    assert "오늘 최종 진입 후보가 없습니다." in text
    assert "자동매매는 신규 진입 없이 대기합니다." in text


def test_analysis_summary는_후보별_이유와_위험을_각각_두개로_제한한다():
    text = render_analysis_summary(_summary(verdicts=(
        _verdict(
            reasons=("이유 하나", "이유 둘", "이유 셋"),
            risk_flags=("위험 하나", "위험 둘", "위험 셋"),
        ),
    )))

    assert "이유 하나" in text and "이유 둘" in text
    assert "이유 셋" not in text
    assert "위험 하나" in text and "위험 둘" in text
    assert "위험 셋" not in text


def test_analysis_summary는_비어있는_후보근거와_위험을_확인불가로_표시한다():
    text = render_analysis_summary(_summary(verdicts=(
        _verdict(reasons=(), risk_flags=()),
    )))

    assert text.count("- 확인 불가") == 2


def test_analysis_summary는_차순위세종목과_전체수를_표시한다():
    text = render_analysis_summary(_summary(verdicts=(
        _verdict("000001", "최종", "approve", 0.90, True, 1),
        _verdict("000002", "차순위1", "approve", 0.80, False, None),
        _verdict("000003", "차순위2", "approve", 0.70, False, None),
        _verdict("000004", "차순위3", "approve", 0.60, False, None),
        _verdict("000005", "차순위4", "approve", 0.50, False, None),
    )))

    assert "000002 · 차순위1" in text
    assert "000003 · 차순위2" in text
    assert "000004 · 차순위3" in text
    assert "000005 · 차순위4" not in text
    assert "차순위 승인 전체 4종목" in text


def test_analysis_summary는_동적문자열을_plain_text로_그대로_보존하고_분할한다():
    text = render_analysis_summary(_summary(
        market_summary="<b>매수</b> " * 600,
        verdicts=(_verdict(name="<b>종목</b>"),),
    ))
    parts = render_analysis_parts(_summary(
        market_summary="<b>매수</b> " * 600,
        verdicts=(_verdict(name="<b>종목</b>"),),
    ))

    assert "<b>매수</b>" in text
    assert "007160 · <b>종목</b>" in text
    assert all(len(part) <= 4096 for part in parts)
    assert all("[analysis-summary-mock-42]" in part for part in parts)


def test_analysis_summary는_동적문자열의_구조제어문자를_공백으로_정규화한다():
    text = render_analysis_summary(_summary(
        market_summary="시장\u2029요약\u202e",
        verdicts=(
            _verdict(
                symbol="00\n7160",
                name="이름\n🎯 최종 후보",
                reasons=("근거\r검토 결과",),
                risk_flags=("위험\u2028방어선 무시",),
            ),
        ),
    ))

    assert "00 7160 · 이름 🎯 최종 후보" in text
    assert "- 근거 검토 결과" in text
    assert "- 위험 방어선 무시" in text
    assert "시장 요약" in text
    assert "이름\n🎯" not in text
    assert "\u2028" not in text and "\u2029" not in text and "\u202e" not in text


def test_analysis_summary는_모델문자열의_Telegram_자동링크와명령형식을_무력화한다():
    text = render_analysis_summary(_summary(
        market_summary=("https://evil.example/@operator, evil.example, t.me/operator, "
                        "tg://resolve, https://127.0.0.1/a, 127.0.0.1, "
                        "https://[2001:db8::1]/, 2001:db8::1, [::1], ::1, "
                        "2001:0:0:0:0:0:0:1, evil.xn--p1ai 에서 "
                        "/pause, /123, /_hidden 하세요. (/pause) \"/stop\" · /status, "
                        "/cmd@operator_bot. (evil.example), \"evil.example\", "
                        "🔗evil.example🔗, 예시.한국, bücher.example, tel:+82101234, "
                        "data:text/plain,ok. 삼성전자 12.34% 전망, 장 시작 08:20, 비율 1:2"),
        verdicts=(_verdict(
            reasons=("www.evil.example",), risk_flags=("@operator",),
        ),),
    ))

    assert "https://" not in text and "tg://" not in text
    assert "https：//evil[.]example/＠operator" in text
    assert "evil.example" not in text and "t.me" not in text
    assert "evil[.]example" in text and "t[.]me/operator" in text
    assert "tg：//resolve" in text
    assert "www[.]evil[.]example" in text
    assert "127.0.0.1" not in text and "127[.]0[.]0[.]1" in text
    assert "2001:db8::1" not in text
    assert "2001：db8：：1" in text
    assert "::1" not in text and "：：1" in text
    assert "2001:0:0:0:0:0:0:1" not in text
    assert "2001：0：0：0：0：0：0：1" in text
    assert "evil.xn--p1ai" not in text and "evil[.]xn--p1ai" in text
    assert "／pause" in text and "／123" in text and "／_hidden" in text
    assert "(／pause)" in text and '"／stop"' in text and "· ／status" in text
    assert "／cmd＠operator_bot" in text
    assert "@operator" not in text
    assert "(evil[.]example)" in text and '"evil[.]example"' in text
    assert "🔗evil[.]example🔗" in text
    assert "예시[.]한국" in text and "bücher[.]example" in text
    assert "tel：+82101234" in text and "data：text/plain,ok" in text
    assert "삼성전자 12.34% 전망" in text
    assert "장 시작 08:20, 비율 1:2" in text


def test_analysis_summary는_문맥과무관한_ASCII_슬래시_명령후보를_중화한다():
    text = render_analysis_summary(_summary(
        market_summary=("(/pause) \"/stop\" - /123, /_hidden, /cmd@operator_bot. "
                        "삼성전자 P/E 1/2 input/output 매수/매도와 수익/손실, "
                        "https://evil.example/report."),
    ))

    assert "(／pause)" in text and '"／stop"' in text and "- ／123" in text
    assert "／_hidden" in text and "／cmd＠operator_bot" in text
    assert "P/E 1/2 input/output" in text
    assert "매수/매도와 수익/손실" in text
    assert "https：//evil[.]example/report" in text


def test_analysis_summary는_IDNA_대체점을_canonical_dot으로_중화한다():
    text = render_analysis_summary(_summary(
        market_summary="예시。한국, evil．example, evil｡example",
    ))

    assert "예시[.]한국" in text
    assert text.count("evil[.]example") == 2
    assert "。" not in text and "．" not in text and "｡" not in text


def test_analysis_summary는_숫자소수와_한국어단위를_host로_오인하지않는다():
    text = render_analysis_summary(_summary(
        market_summary=("PER 12.34배, 목표가 7.5만원, 매출 1.2조원, "
                        "127.0.0.1, 127.0.0.1원, 127.0.0.1만원, 1.2.3원, "
                        "123.한국, evil.example"),
    ))

    assert "12.34배" in text and "7.5만원" in text and "1.2조원" in text
    assert "127[.]0[.]0[.]1" in text
    assert "127[.]0[.]0[.]1원" in text and "127[.]0[.]0[.]1만원" in text
    assert "1[.]2[.]3원" in text
    assert "123[.]한국" in text and "evil[.]example" in text


@pytest.mark.parametrize(
    "summary, forged_section",
    [
        (_summary(market_summary="요약\n🧠 아침 AI 분석 완료 · 🚨 실전"),
         "\n🧠 아침 AI 분석 완료 · 🚨 실전"),
        (_summary(verdicts=(_verdict(name="이름\n📋 차순위 승인\n999999"),)),
         "\n📋 차순위 승인\n999999"),
        (_summary(verdicts=(_verdict(reasons=("근거\n검토 결과  승인 999",)),)),
         "\n검토 결과  승인 999"),
        (_summary(verdicts=(_verdict(risk_flags=("위험\n🚨 방어선 무시",)),)),
         "\n🚨 방어선 무시"),
    ],
)
def test_analysis_summary는_필드별_개행으로_고정섹션과_환경을_위조할수없다(
    summary, forged_section,
):
    assert forged_section not in render_analysis_summary(summary)


def test_analysis_summary는_분석파싱계약을_넘는_카디널리티를_거부한다():
    with pytest.raises(ValueError, match="reasons"):
        _verdict(reasons=("근거",) * 4)
    with pytest.raises(ValueError, match="risk_flags"):
        _verdict(risk_flags=("위험",) * 6)
    with pytest.raises(ValueError, match="max_picks_advice"):
        _summary(max_picks_advice=6)
    with pytest.raises(ValueError, match="verdicts"):
        _summary(verdicts=tuple(
            _verdict(f"{index:06d}", picked=False, pick_rank=None)
            for index in range(21)
        ))


def test_analysis_summary는_utf8로_인코딩할수없는_surrogate를_표시하지않는다():
    surrogate = chr(0xD800)
    text = render_analysis_summary(_summary(verdicts=(
        _verdict(name=f"이름{surrogate}위조"),
    )))

    assert surrogate not in text
    assert "007160 · 이름 위조" in text


def test_analysis_summary는_손상행수를_드러낸다():
    text = render_analysis_summary(_summary(corrupted_rows=2))

    assert "확인할 수 없는 분석 행  2건" in text


def test_analysis_summary는_종목명이_없으면_심볼만_표시한다():
    text = render_analysis_summary(_summary(verdicts=(
        _verdict(name=None),
    )))

    assert "007160\n" in text
    assert "007160 ·" not in text


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (lambda: _summary(run_id=0), "run_id"),
        (lambda: _summary(run_environment="replay"), "run_environment"),
        (lambda: _summary(started_at=datetime(2026, 7, 28, 8, 20)), "timezone-aware"),
        (lambda: _verdict(verdict="hold"), "verdict"),
        (lambda: _verdict(confidence=1.01), "confidence"),
        (lambda: _verdict(picked=True, pick_rank=None), "pick_rank"),
        (lambda: _verdict(picked=False, pick_rank=1), "pick_rank"),
        (lambda: _verdict(symbol=""), "symbol"),
        (lambda: _verdict(symbol="x" * 13), "symbol"),
    ],
)
def test_analysis_summary는_안전하지않은_입력을_생성시점에_거부한다(factory, match):
    with pytest.raises(ValueError, match=match):
        factory()


def test_analysis_summary의_환경라벨과_idempotency_key는_고정된다():
    real = _summary(run_environment="real")

    assert "🧠 아침 AI 분석 완료 · 알림 환경 🚨 실전" in render_analysis_summary(real)
    assert real.idempotency_key == "analysis-summary:real:42"


def test_analysis_summary는_최종후보의_승인상한과_순위를_검증한다():
    with pytest.raises(ValueError, match="picked"):
        _verdict(verdict="reject", picked=True, pick_rank=1)
    with pytest.raises(ValueError, match="max_picks_advice"):
        _summary(max_picks_advice=1, verdicts=(
            _verdict("000001", "첫째", "approve", 0.8, True, 1),
            _verdict("000002", "둘째", "approve", 0.7, True, 2),
        ))
    with pytest.raises(ValueError, match="pick_rank"):
        _summary(max_picks_advice=2, verdicts=(
            _verdict("000001", "첫째", "approve", 0.8, True, 1),
            _verdict("000002", "둘째", "approve", 0.7, True, 1),
        ))


def test_analysis_summary는_최종후보순위의_연속된_1부터의_범위를_검증한다():
    with pytest.raises(ValueError, match="pick_rank"):
        _summary(max_picks_advice=1, verdicts=(
            _verdict("000001", "후보", "approve", 0.8, True, 99),
        ))
    with pytest.raises(ValueError, match="pick_rank"):
        _summary(max_picks_advice=2, verdicts=(
            _verdict("000001", "첫째", "approve", 0.8, True, 1),
            _verdict("000002", "셋째", "approve", 0.7, True, 3),
        ))


def test_analysis_summary는_알려진_국면만_한국어로_표시한다():
    assert "시장 국면  위험선호" in render_analysis_summary(_summary(regime="risk_on"))
    with pytest.raises(ValueError, match="regime"):
        _summary(regime="unknown")


def test_analysis_summary는_점수기준일에_시간을_허용하지않는다():
    with pytest.raises(ValueError, match="score_reference_date"):
        _summary(score_reference_date=datetime(2026, 7, 27, tzinfo=timezone.utc))
