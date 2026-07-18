"""Phase 4 실환경 스모크 — 호스트 Ollama(gemma4:31b-cloud)와 네이버 뉴스
검색 API를 실제로 호출한다. 실행: uv run pytest -m live tests/live/test_live_analysis_smoke.py -v

전제(스펙 §11): Ollama 설치 + `ollama signin`(클라우드 모델) + 모델 등록,
backend/.env에 NAVER_CLIENT_ID/NAVER_CLIENT_SECRET. 전제가 빠지면 실패가
아니라 skip으로 처리한다 — 키움 스모크의 KIWOOM_MOCK 게이트와 같은 관례."""

import json

import httpx
import pytest

from app.core.config import Settings
from app.domain.analysis.config import AnalysisConfig
from app.adapters.naver.client import NaverNewsClient
from app.adapters.ollama.client import OllamaClient

pytestmark = pytest.mark.live

# base_url 기본값(host.docker.internal)은 컨테이너 관점이다 — 호스트에서
# 그 이름은 LAN IP로 풀리는데 Ollama는 기본 127.0.0.1 바인딩이라 연결이
# 거부된다(실측). 이 스모크는 호스트 실행이므로 루프백을 쓰고, 컨테이너 →
# 호스트 경로는 T7 Step 4의 end-to-end에서 별도 검증한다.
_OLLAMA_HOST_URL = "http://127.0.0.1:11434"


@pytest.fixture
def settings() -> Settings:
    return Settings()  # .env에서 로드


@pytest.fixture
def ollama_url() -> str:
    """전제(데몬 기동) 미충족은 실패가 아니라 skip — 키움 스모크의
    KIWOOM_MOCK 게이트와 같은 관례(모듈 docstring)."""
    try:
        httpx.get(f"{_OLLAMA_HOST_URL}/api/tags", timeout=3)
    except httpx.HTTPError:
        pytest.skip("호스트 Ollama 데몬에 연결 불가 - 스펙 §11 준비물")
    return _OLLAMA_HOST_URL


@pytest.mark.anyio
async def test_live_ollama_generate_json은_유효한_JSON을_반환한다(ollama_url):
    """어댑터 경로 전체(호스트 데몬 → 클라우드 추론 → format=json 응답)를
    실측한다 — 파이프라인이 기대하는 '파싱 가능한 JSON 문자열' 계약 검증."""
    cfg = AnalysisConfig()
    llm = OllamaClient(ollama_url, cfg.model, cfg.temperature,
                       cfg.llm_timeout_s)
    try:
        raw = await llm.generate_json(
            "너는 JSON만 출력하는 도우미다.",
            '{"ok": true} 형태로 키 "ok"에 불리언 true를 담은 JSON 객체만 출력하라.')
        parsed = json.loads(raw)  # 유효 JSON이 아니면 여기서 실패
        assert isinstance(parsed, dict)
        print(f"[live] ollama model={cfg.model} keys={sorted(parsed)}")
    finally:
        await llm.aclose()


@pytest.mark.anyio
async def test_live_네이버_코스피_헤드라인_1건_이상(settings):
    if settings.naver_client_id is None or settings.naver_client_secret is None:
        pytest.skip("NAVER_CLIENT_ID/NAVER_CLIENT_SECRET 미설정 - 스펙 §11 준비물")
    news = NaverNewsClient(settings.naver_client_id, settings.naver_client_secret)
    try:
        headlines = await news.search_headlines("코스피", limit=5)
        assert len(headlines) >= 1
        # 제목 정제(_clean_title) 계약: 태그/엔티티가 남아 있으면 안 된다.
        assert all("<b>" not in h.title and "&quot;" not in h.title
                   for h in headlines)
        print(f"[live] naver headlines={len(headlines)} "
              f"first={headlines[0].title[:40]!r}")
    finally:
        await news.aclose()
