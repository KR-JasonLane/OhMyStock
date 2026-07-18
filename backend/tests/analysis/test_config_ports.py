import json

from app.domain.analysis.config import AnalysisConfig
from app.domain.analysis.ports import Headline, LlmPort, NewsPort


def test_config_기본값과_스냅샷():
    cfg = AnalysisConfig()
    assert (cfg.model, cfg.max_picks, cfg.score_max_age_days) == \
        ("exaone3.5:7.8b", 5, 3)
    snap = json.loads(cfg.to_json())
    assert snap["market_keywords"] == ["코스피", "코스닥", "증시"]
    assert cfg.to_json() == cfg.to_json()  # 결정론


def test_포트는_런타임_체크_가능():
    class FakeLlm:
        async def generate_json(self, system, prompt):
            return "{}"

    class FakeNews:
        async def search_headlines(self, query, limit):
            return [Headline("t", "u", "d")]

    assert isinstance(FakeLlm(), LlmPort)
    assert isinstance(FakeNews(), NewsPort)
