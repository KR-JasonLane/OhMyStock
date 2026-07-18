import pytest
import respx
from pydantic import SecretStr

from app.adapters.naver.client import NaverNewsClient
from app.domain.analysis.ports import NewsError, NewsPort

URL = "https://openapi.naver.com/v1/search/news.json"


def make_client():
    return NaverNewsClient(client_id=SecretStr("cid"),
                           client_secret=SecretStr("csec"))


@pytest.mark.anyio
@respx.mock
async def test_헤드라인_매핑과_태그제거():
    route = respx.get(URL).respond(json={"items": [
        {"title": "<b>삼성전자</b> 신고가 &quot;돌파&quot;",
         "originallink": "https://news.example/1", "link": "https://naver/1",
         "pubDate": "Fri, 17 Jul 2026 09:00:00 +0900"},
        {"title": "무링크", "originallink": "", "link": "https://naver/2",
         "pubDate": "d2"},
    ]})
    client = make_client()
    out = await client.search_headlines("삼성전자", limit=5)
    assert out[0].title == '삼성전자 신고가 "돌파"'
    assert out[0].url == "https://news.example/1"
    assert out[1].url == "https://naver/2"        # originallink 없으면 link
    req = route.calls[0].request
    assert req.headers["X-Naver-Client-Id"] == "cid"
    assert "display=5" in str(req.url) and "sort=date" in str(req.url)
    await client.aclose()


@pytest.mark.anyio
@respx.mock
async def test_비2xx는_NewsError():
    respx.get(URL).respond(status_code=429)
    client = make_client()
    with pytest.raises(NewsError):
        await client.search_headlines("q", limit=5)
    await client.aclose()


def test_NewsPort_구현():
    assert isinstance(make_client(), NewsPort)
