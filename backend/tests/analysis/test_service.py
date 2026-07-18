"""AnalysisService 오케스트레이션 검증 — 가짜 store/llm/news, 고정 기준일.

FakeStore는 AnalysisStore(app/store/analysis_store.py)의 실제 시그니처를
그대로 미러링한다 — create_run/finish_run/save_results/load_candidates/
market_snapshot/latest_succeeded_score_run 인자 순서가 어긋나면 이 테스트가
실서비스보다 먼저 깨진다."""

import json
from datetime import date, timedelta

import pytest

from app.domain.analysis.config import AnalysisConfig
from app.domain.analysis.ports import (CandidateInput, Headline, LlmError,
                                       MarketSnapshot, NewsError,
                                       StrategyDetailInput)
from app.domain.analysis.service import AnalysisService

REF = date(2026, 7, 17)
CFG = AnalysisConfig(parse_retries=1, market_keywords=("코스피",),
                     news_per_symbol=3, score_max_age_days=3)


def cand(symbol, name, total):
    return CandidateInput(symbol=symbol, name=name, sector_name="s",
                          total_score=total, sector_score=0.5,
                          strategy_score_norm=0.5,
                          details=(StrategyDetailInput("momentum", True,
                                                       0.05, 0.6, 3),))


class ScriptedLlm:
    """호출 순서대로 응답을 소진하는 가짜 — economist가 첫 호출
    (tests/analysis/test_graph.py와 동일 패턴, 파일 간 결합을 피하려고 복제)."""

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


class FakeNews:
    """query -> 헤드라인 목록 또는 예외 인스턴스. 미등록 query는 빈 목록."""

    def __init__(self, responses: dict | None = None):
        self._responses = responses or {}
        self.calls: list[tuple[str, int]] = []

    async def search_headlines(self, query, limit):
        self.calls.append((query, limit))
        resp = self._responses.get(query, [])
        if isinstance(resp, Exception):
            raise resp
        return resp


class FakeStore:
    """AnalysisStore 실제 시그니처 미러(주석 상단 참고)."""

    def __init__(self, score_run=(7, REF), candidates=None, snapshot=None):
        self._score_run = score_run
        self._candidates = candidates if candidates is not None else []
        self._snapshot = snapshot if snapshot is not None else MarketSnapshot(
            sector_table="음식료 0.0100 0.0200 0.0300", breadth=0.6)
        self.created = None
        self.finished = None  # (run_id, status, regime, market_summary, warnings, failure_reason)
        self.saved = None     # (run_id, result, news)

    def latest_succeeded_score_run(self):
        return self._score_run

    def load_candidates(self, score_run_id):
        return self._candidates

    def market_snapshot(self, score_run_id):
        return self._snapshot

    def create_run(self, score_run_id, model, prompt_hash, config_json):
        self.created = (score_run_id, model, prompt_hash, config_json)
        return 42

    def finish_run(self, run_id, status, regime=None, market_summary=None,
                   warnings=None, failure_reason=None):
        self.finished = (run_id, status, regime, market_summary, warnings,
                         failure_reason)

    def save_results(self, run_id, result, news):
        self.saved = (run_id, result, news)

    def latest_results(self):
        return {"sentinel": True}


@pytest.mark.anyio
async def test_성공_경로_결과_저장과_뉴스_스냅샷():
    candidates = [cand("AAA111", "가나다전자", 0.9), cand("BBB222", "동양", 0.8)]
    store = FakeStore(candidates=candidates)
    llm = ScriptedLlm([ECON_OK, approve(0.9), approve(0.5)])
    news = FakeNews({
        "코스피": [Headline("코스피 상승", "http://a", "2026-07-17")],
        "가나다전자 주가": [Headline("가나다전자 실적", "http://b", "2026-07-17")],
        "동양 주가": [Headline("동양 그룹 소식", "http://c", "2026-07-17")],
    })
    service = AnalysisService(store, llm, news, config=CFG, today=lambda: REF)

    await service.run()

    assert store.finished[1] == "succeeded"
    assert store.finished[2] == "neutral"  # regime
    assert store.finished[4] == "[]"       # no warnings -> empty JSON array
    run_id, result, saved_news = store.saved
    assert run_id == 42
    assert set(saved_news) == {"market", "AAA111", "BBB222"}
    # 동음이의 종목명 뉴스 완화 (carry-over #2) — "{name} 주가"로 검색
    assert ("가나다전자 주가", CFG.news_per_symbol) in news.calls
    assert ("동양 주가", CFG.news_per_symbol) in news.calls
    assert service.is_running() is False


@pytest.mark.anyio
async def test_게이트_스코어링_런_없음():
    store = FakeStore(score_run=None)
    llm = ScriptedLlm([])
    service = AnalysisService(store, llm, None, config=CFG, today=lambda: REF)

    await service.run()

    # 아직 run이 만들어지지 않았으므로 finish_run은 호출되지 않는다.
    assert store.finished is None
    assert store.created is None
    progress = service.progress()
    assert progress.status == "failed"
    assert "run scoring first" in progress.failure_reason
    assert llm.calls == []


@pytest.mark.anyio
async def test_게이트_낡은_스코어링():
    stale_ref = REF - timedelta(days=4)
    store = FakeStore(score_run=(7, stale_ref))
    service = AnalysisService(store, ScriptedLlm([]), None, config=CFG,
                              today=lambda: REF)

    await service.run()

    assert store.finished is None
    progress = service.progress()
    assert progress.status == "failed"
    assert "stale" in progress.failure_reason
    assert stale_ref.isoformat() in progress.failure_reason


@pytest.mark.anyio
async def test_게이트_빈_섹터_테이블은_LLM_호출_없이_실패():
    """트레이더 게이트(carry-over #1, MUST) — breadth 0.0이 '데이터 없음'인지
    '전 업종 하락'인지 구분 불가하므로 LLM을 호출하지 않고 보수적으로 실패."""
    candidates = [cand("AAA111", "가전", 0.9)]
    store = FakeStore(candidates=candidates,
                      snapshot=MarketSnapshot(sector_table="", breadth=0.0))
    llm = ScriptedLlm([])
    service = AnalysisService(store, llm, None, config=CFG, today=lambda: REF)

    await service.run()

    assert store.finished[1] == "failed"
    assert "no sector aggregates" in store.finished[5]
    assert llm.calls == []
    assert store.saved is None


@pytest.mark.anyio
async def test_뉴스_포트_없으면_경고와_함께_진행():
    candidates = [cand("AAA111", "가전", 0.9)]
    store = FakeStore(candidates=candidates)
    llm = ScriptedLlm([ECON_OK, approve(0.9)])
    service = AnalysisService(store, llm, None, config=CFG, today=lambda: REF)

    await service.run()

    assert store.finished[1] == "succeeded"
    warnings = json.loads(store.finished[4])
    assert warnings == ["news skipped (no naver keys)"]
    run_id, result, saved_news = store.saved
    assert saved_news == {"market": []}


@pytest.mark.anyio
async def test_뉴스_실패는_건별_경고_지속():
    candidates = [cand("AAA111", "가전", 0.9), cand("BBB222", "나다전자", 0.8)]
    store = FakeStore(candidates=candidates)
    llm = ScriptedLlm([ECON_OK, approve(0.9), approve(0.5)])
    news = FakeNews({
        "코스피": NewsError("timeout"),
        "가전 주가": [Headline("t", "u", "d")],
        "나다전자 주가": [Headline("t2", "u2", "d2")],
    })
    service = AnalysisService(store, llm, news, config=CFG, today=lambda: REF)

    await service.run()

    assert store.finished[1] == "succeeded"
    warnings = json.loads(store.finished[4])
    assert any("코스피" in w for w in warnings)
    run_id, result, saved_news = store.saved
    assert set(saved_news) == {"market", "AAA111", "BBB222"}
    assert saved_news["market"] == []


@pytest.mark.anyio
async def test_파싱_폴백_경고가_뉴스_경고와_병합되어_저장된다():
    """이월(carry-over #3) — economist/trader 파싱 폴백 경고가 뉴스 경고와
    합류해 finish_run에 전파돼야 반복 폴백을 감사할 수 있다."""
    candidates = [cand("AAA111", "가나다전자", 0.9)]
    store = FakeStore(candidates=candidates)
    llm = ScriptedLlm(["broken", "broken", approve(0.9)])  # economist 2회 실패 → 폴백
    news = FakeNews({"코스피": [Headline("t", "u", "d")],
                     "가나다전자 주가": NewsError("timeout")})
    service = AnalysisService(store, llm, news, config=CFG, today=lambda: REF)

    await service.run()

    assert store.finished[1] == "succeeded"
    warnings = json.loads(store.finished[4])
    assert "economist-parse-fallback" in warnings
    assert any("AAA111" in w for w in warnings)


@pytest.mark.anyio
async def test_LlmError는_런_실패():
    candidates = [cand("AAA111", "가전", 0.9)]
    store = FakeStore(candidates=candidates)
    llm = ScriptedLlm([LlmError("ollama down")])
    news = FakeNews({"코스피": []})
    service = AnalysisService(store, llm, news, config=CFG, today=lambda: REF)

    await service.run()

    assert store.finished[1] == "failed"
    reason = store.finished[5]
    assert "ollama down" in reason
    # 부분 결과(mid-traders) 유실 경고 (carry-over #7)
    assert "재실행" in reason
    assert store.saved is None


@pytest.mark.anyio
async def test_예상치_못한_예외도_run을_failed로_마감한다():
    """T7 패널이 요구했던 형제 테스트 — 처음부터 포함."""

    class ExplodingStore(FakeStore):
        def save_results(self, run_id, result, news):
            raise RuntimeError("db exploded")

    candidates = [cand("AAA111", "가전", 0.9)]
    store = ExplodingStore(candidates=candidates)
    llm = ScriptedLlm([ECON_OK, approve(0.9)])
    news = FakeNews({"코스피": []})
    service = AnalysisService(store, llm, news, config=CFG, today=lambda: REF)

    with pytest.raises(RuntimeError):
        await service.run()

    assert store.finished[1] == "failed"
    assert store.finished[5].startswith("unexpected:")
    assert service.is_running() is False


@pytest.mark.anyio
async def test_start는_중복_거부():
    candidates = [cand("AAA111", "가전", 0.9)]
    store = FakeStore(candidates=candidates)
    llm = ScriptedLlm([ECON_OK, approve(0.9)])
    news = FakeNews({"코스피": []})
    service = AnalysisService(store, llm, news, config=CFG, today=lambda: REF)

    task = service.start()
    assert task is not None
    assert service.start() is None
    await task


def test_latest_results는_store에_위임한다():
    store = FakeStore()
    service = AnalysisService(store, ScriptedLlm([]), None, config=CFG,
                              today=lambda: REF)
    assert service.latest_results() == {"sentinel": True}
