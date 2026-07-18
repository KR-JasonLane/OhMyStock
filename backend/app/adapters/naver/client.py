"""네이버 뉴스 검색(`/v1/search/news.json`) 클라이언트 — 도메인의 `NewsPort`의
구조적 구현(명시적 상속 없음 — kiwoom 어댑터 관례).

kiwoom `client.py`와 동일한 소유권 계약을 따른다: 이 클래스가 생성한
`httpx.AsyncClient`는 이 클래스가 `aclose()`로 닫는다. 외부에서 주입된
`http`는 이 클래스가 닫지 않는다(호출자 책임).

시크릿(`client_id`/`client_secret`)은 헤더 조립 시점에만 `get_secret_value()`로
꺼내며, 로그에 남기지 않는다.
"""

import html
import re

import httpx
from pydantic import SecretStr

from app.domain.analysis.ports import Headline, NewsError

_BASE_URL = "https://openapi.naver.com"
_SEARCH_PATH = "/v1/search/news.json"
_TAG_RE = re.compile(r"</?b>")


def _clean_title(title: str) -> str:
    return html.unescape(_TAG_RE.sub("", title))


class NaverNewsClient:
    def __init__(
        self,
        client_id: SecretStr,
        client_secret: SecretStr,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(base_url=_BASE_URL, timeout=10.0)

    async def search_headlines(self, query: str, limit: int) -> list[Headline]:
        headers = {
            "X-Naver-Client-Id": self._client_id.get_secret_value(),
            "X-Naver-Client-Secret": self._client_secret.get_secret_value(),
        }
        params = {"query": query, "display": limit, "sort": "date"}
        try:
            resp = await self._http.get(_SEARCH_PATH, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise NewsError(
                f"네이버 뉴스 검색 접속 실패: {type(exc).__name__}"
            ) from exc

        if resp.status_code < 200 or resp.status_code >= 300:
            raise NewsError(f"네이버 뉴스 검색 응답 오류 http={resp.status_code}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise NewsError("네이버 뉴스 검색 비-JSON 응답") from exc

        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise NewsError("네이버 뉴스 검색 응답에 items가 없습니다")

        return [
            Headline(
                title=_clean_title(item.get("title", "")),
                url=item.get("originallink") or item.get("link", ""),
                published_at=item.get("pubDate", ""),
            )
            for item in items
        ]

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
