"""AnalysisService 오케스트레이션 검증 — 가짜 store/llm/news, 고정 기준일.

FakeStore는 AnalysisStore(app/store/analysis_store.py)의 실제 시그니처를
그대로 미러링한다 — create_run/finish_run/save_results/load_candidates/
market_snapshot/latest_succeeded_score_run 인자 순서가 어긋나면 이 테스트가
실서비스보다 먼저 깨진다.

P4-T5 패널 픽스(변경 근거): stale 게이트가 create_run **이후**로 이동해
(score_run_id 확보 후 감사 이력에 실패로 남김) `test_게이트_낡은_스코어링`의
"store.finished is None" 단언이 뒤집혔다. `_fail`이 실패 시점의 stage를
보존하도록 바뀌어(더 이상 항상 "finished"로 뭉개지 않음) 여러 실패 테스트에
`progress().stage` 단언을 추가했다. `run_id`가 `int | None`이 되어(None =
런 미생성, 스코어링 런 자체가 없는 경우 한정) 해당 테스트에 명시 단언을
추가했다."""

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
    assert progress.run_id is None  # 런 미생성 — score_run_id가 없어 FK를
                                     # 채울 수 없는 유일한 경우 (계약)
    assert progress.status == "failed"
    assert progress.stage == "gate"
    assert "run scoring first" in progress.failure_reason
    assert llm.calls == []


@pytest.mark.anyio
async def test_게이트_낡은_스코어링():
    """score_run_id는 이미 확보돼 있으므로(스코어링 런은 존재, 낡았을
    뿐) create_run이 먼저 실행되고 _fail이 DB에 실패로 기록한다 — 런
    자체가 없는 게이트(위 테스트)와 비대칭(service.py `_run()` docstring
    (b) 참고)."""
    stale_ref = REF - timedelta(days=4)
    store = FakeStore(score_run=(7, stale_ref))
    service = AnalysisService(store, ScriptedLlm([]), None, config=CFG,
                              today=lambda: REF)

    await service.run()

    assert store.created is not None  # run은 생성됨 (score_run_id 확보 후)
    assert store.finished is not None
    assert store.finished[0] == 42
    assert store.finished[1] == "failed"
    progress = service.progress()
    assert progress.run_id == 42
    assert progress.status == "failed"
    assert progress.stage == "gate"
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
    assert service.progress().stage == "gate"


@pytest.mark.anyio
async def test_게이트_후보_없음_LLM_호출_없이_실패():
    """트레이더 패널 minor(#8) — 후보 0건(스코어링 런에 남은 후보가 없는
    경우)도 빈 섹터 표 게이트와 동일하게 LLM을 호출하지 않고 보수적으로
    실패해야 한다."""
    store = FakeStore(candidates=[])
    llm = ScriptedLlm([])
    service = AnalysisService(store, llm, None, config=CFG, today=lambda: REF)

    await service.run()

    assert store.finished[1] == "failed"
    assert "no candidates" in store.finished[5]
    assert llm.calls == []
    assert store.saved is None
    assert service.progress().stage == "gate"
    assert service.progress().run_id == 42


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
    # LlmError는 파이프라인 실행 구간에서만 발생하고, 그 구간 전체가
    # stage="economist"로 표시된다 (스펙 §7 addendum) — _fail이 실패
    # 시점 stage를 보존하도록 바뀐 뒤의 계약(T5 패널 픽스).
    assert service.progress().stage == "economist"


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
    # 예외는 save_results 호출 중(stage="synthesize"로 설정된 뒤) 발생 —
    # _fail이 그 시점 stage를 보존한다(T5 패널 픽스).
    assert service.progress().stage == "synthesize"
    assert service.is_running() is False


@pytest.mark.anyio
async def test_LangSmith_가드_RuntimeError는_economist_단계_사유로_보존된다(
        monkeypatch):
    """아키텍트 패널(#5) — AnalysisPipeline 생성자의 LangSmith 텔레메트리
    가드(graph.py, RuntimeError)를 service.py가 좁게 잡아 사유를
    failure_reason에 그대로 남긴다. 좁게 잡지 않으면 바깥 `except
    Exception`이 삼켜 "unexpected: RuntimeError"로 뭉개져 어떤 env var가
    문제인지 알 수 없게 된다."""
    monkeypatch.setenv("LANGSMITH_TRACING_V2", "true")
    candidates = [cand("AAA111", "가전", 0.9)]
    store = FakeStore(candidates=candidates)
    llm = ScriptedLlm([])  # economist까지 도달하지 못하므로 호출 없음
    service = AnalysisService(store, llm, None, config=CFG, today=lambda: REF)

    await service.run()

    assert store.finished[1] == "failed"
    reason = store.finished[5]
    assert "LangSmith" in reason
    assert "활성화 금지" in reason
    assert llm.calls == []
    assert store.saved is None
    assert service.progress().stage == "economist"


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


@pytest.mark.anyio
async def test_create_run_직후_progress에_run_id가_즉시_반영된다():
    """T5 아키텍트 재검증 지적 — create_run 이후 게이트/로딩 구간에서 DB에는
    run이 실재하는데 progress.run_id가 None("런 미생성")으로 남는 계약 위반
    시간창 회귀 방지. load_candidates에서 블로킹시켜 그 구간의 progress를
    동시 폴링으로 관측한다."""
    import asyncio as _asyncio

    release = _asyncio.Event()

    class BlockingStore(FakeStore):
        def load_candidates(self, score_run_id):
            # 동기 store 호출은 to_thread로 실행되므로 이벤트 루프는 계속
            # 돈다 — 워커 스레드에서 release가 켜질 때까지 대기해 블로킹
            # 구간을 만든다.
            import time
            while not release.is_set():
                time.sleep(0.01)
            return [cand("AAA111", "가전", 0.9)]

    store = BlockingStore()
    llm = ScriptedLlm([ECON_OK, approve(0.9)])
    news = FakeNews({"코스피": []})
    service = AnalysisService(store, llm, news, config=CFG, today=lambda: REF)

    task = service.start()
    assert task is not None
    # create_run + 게이트 통과 후 load_candidates 블로킹 구간에 진입할 때까지
    # 짧게 양보하며 progress가 실제 run_id(42)로 동기화됐는지 관측한다.
    for _ in range(100):
        await _asyncio.sleep(0.01)
        progress = service.progress()
        if progress is not None and progress.run_id is not None:
            break
    assert service.progress().run_id == 42
    assert service.progress().status == "running"
    release.set()
    await task
    assert service.progress().status == "succeeded"
