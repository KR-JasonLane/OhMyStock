"""Ollama `/api/generate` 클라이언트 — 도메인의 `LlmPort` 구현.

kiwoom `client.py`와 동일한 소유권 계약을 따른다: 이 클래스가 생성한
`httpx.AsyncClient`는 이 클래스가 `aclose()`로 닫는다. 외부에서 주입된
`http`는 이 클래스가 닫지 않는다(호출자 책임).
"""

import httpx

from app.domain.analysis.ports import LlmError

_GUIDE = "Ollama가 설치·기동됐는지, 모델이 pull됐는지 확인하세요."


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
        return response

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
