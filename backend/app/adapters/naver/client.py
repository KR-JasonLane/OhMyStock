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
    # 인코딩된 엔티티(예: &lt;b&gt;)가 태그 제거 후에도 잔존하지 않도록
    # unescape를 먼저 수행한 뒤 태그를 제거한다(방어 심층).
    return _TAG_RE.sub("", html.unescape(title))


class NaverNewsClient:
    def __init__(
        self,
        client_id: SecretStr,
        client_secret: SecretStr,
        *,
        timeout_s: float = 10.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(base_url=_BASE_URL, timeout=timeout_s)

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

        if not resp.is_success:
            raise NewsError(f"네이버 뉴스 검색 응답 오류 http={resp.status_code}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise NewsError("네이버 뉴스 검색 비-JSON 응답") from exc

        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise NewsError("네이버 뉴스 검색 응답에 items가 없습니다")

        try:
            return [
                Headline(
                    title=_clean_title(item.get("title", "")),
                    url=item.get("originallink") or item.get("link", ""),
                    published_at=item.get("pubDate", ""),
                )
                for item in items
            ]
        except (AttributeError, TypeError) as exc:
            # items 원소가 dict가 아니면(문자열 등) .get() 호출이 벤더 예외를
            # 던진다 — NewsPort 계약(NewsError만 누출)을 지키기 위해 변환.
            raise NewsError("네이버 뉴스 검색 응답의 items 원소 형식이 올바르지 않습니다") from exc

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
