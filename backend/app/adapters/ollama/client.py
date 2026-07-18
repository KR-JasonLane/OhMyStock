"""Ollama `/api/generate` 클라이언트 — 도메인의 `LlmPort` 구현.

kiwoom `client.py`와 동일한 소유권 계약을 따른다: 이 클래스가 생성한
`httpx.AsyncClient`는 이 클래스가 `aclose()`로 닫는다. 외부에서 주입된
`http`는 이 클래스가 닫지 않는다(호출자 책임).
"""

import re

import httpx

from app.domain.analysis.ports import LlmError

_GUIDE = "Ollama가 설치·기동됐는지, 모델이 pull됐는지 확인하세요."

# 클라우드 추론 경로(예: gemma4:31b-cloud → ollama.com 원격)는 `format:
# "json"` 제약을 무시하고 응답을 마크다운 펜스(```json ... ```)로 감싸서
# 반환한다 — Phase 4 수용 검증(2026-07-18)에서 실측. 펜스는 전송/모델
# 아티팩트이므로 어댑터가 벗겨서 `LlmPort`의 "JSON 문자열 반환" 계약을
# 지킨다(도메인 파싱은 엄격한 계약 유지). 벗기는 것은 여닫는 백틱 개수가
# 같은 대칭 펜스 한 겹뿐이다(백틱 4개 이상 펜스는 역참조로 같은 개수만
# 매치 — 고정 3개 매치는 잔여 백틱이 본문을 오염시킨다, T7 개발자 패널).
# 비대칭·무펜스 응답은 원문 그대로 통과해 도메인 파싱이 fail-loud로
# 걸러낸다. 빈 펜스(```json\n```)는 빈 문자열이 되며 이 역시 다운스트림
# json.loads가 fail-loud.
# `(?!`)`가 없으면 백트래킹이 여는 백틱 일부만 fence로 소비해(4개 중
# 3개) 비대칭 펜스를 "벗기다 만" 오염 문자열로 만든다 — 여는 펜스는
# 반드시 연속 백틱 전부를 소비해야 한다.
_FENCE_RE = re.compile(
    r"^(?P<fence>`{3,})(?!`)(?:json)?\s*\n?(?P<body>.*?)\n?(?P=fence)\s*$",
    re.DOTALL)


def _strip_markdown_fence(text: str) -> str:
    match = _FENCE_RE.match(text.strip())
    return match.group("body") if match else text


class OllamaClient:
    """`LlmPort`의 구조적 구현 (명시적 상속 없음 — kiwoom 어댑터 관례)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        temperature: float,
        timeout_s: float,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url
        self._model = model
        self._temperature = temperature
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    async def generate_json(self, system: str, prompt: str) -> str:
        body = {
            "model": self._model,
            "system": system,
            "prompt": prompt,
            "format": "json",
            "stream": False,
            "options": {"temperature": self._temperature},
        }
        try:
            resp = await self._http.post("/api/generate", json=body)
        except httpx.HTTPError as exc:
            raise LlmError(
                f"Ollama 접속 실패 [{self._base_url}] ({type(exc).__name__}). "
                f"{_GUIDE}"
            ) from exc

        if not resp.is_success:
            raise LlmError(
                f"Ollama 응답 오류 [{self._base_url}] "
                f"http={resp.status_code}. {_GUIDE}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise LlmError(
                f"Ollama 비-JSON 응답 [{self._base_url}]. {_GUIDE}"
            ) from exc

        if not isinstance(data, dict) or "response" not in data:
            raise LlmError(
                f"Ollama 응답에 response 키가 없습니다 [{self._base_url}]. {_GUIDE}"
            )
        response = data["response"]
        if not isinstance(response, str):
            raise LlmError(
                f"Ollama 응답의 response가 문자열이 아닙니다 [{self._base_url}]. "
                f"{_GUIDE}"
            )
        return _strip_markdown_fence(response)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
