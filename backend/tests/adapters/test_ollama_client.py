import httpx
import pytest
import respx

from app.adapters.ollama.client import OllamaClient
from app.domain.analysis.ports import LlmError, LlmPort

BASE = "http://host.docker.internal:11434"


def make_client():
    return OllamaClient(base_url=BASE, model="exaone3.5:7.8b",
                        temperature=0.2, timeout_s=5)


@pytest.mark.anyio
@respx.mock
async def test_generate_json_요청_형식과_응답():
    route = respx.post(f"{BASE}/api/generate").respond(
        json={"response": '{"ok": true}', "done": True})
    client = make_client()
    out = await client.generate_json("시스템", "프롬프트")
    assert out == '{"ok": true}'
    body = route.calls[0].request.content
    import json
    sent = json.loads(body)
    assert sent["model"] == "exaone3.5:7.8b"
    assert sent["format"] == "json" and sent["stream"] is False
    assert sent["options"]["temperature"] == 0.2
    await client.aclose()


@pytest.mark.anyio
@respx.mock
async def test_접속불가는_LlmError_안내포함():
    respx.post(f"{BASE}/api/generate").mock(
        side_effect=httpx.ConnectError("refused"))
    client = make_client()
    with pytest.raises(LlmError) as exc:
        await client.generate_json("s", "p")
    assert "Ollama" in str(exc.value) and BASE in str(exc.value)
    await client.aclose()


@pytest.mark.anyio
@respx.mock
async def test_비2xx와_키부재는_LlmError():
    respx.post(f"{BASE}/api/generate").respond(status_code=500)
    client = make_client()
    with pytest.raises(LlmError):
        await client.generate_json("s", "p")
    await client.aclose()

    with respx.mock:
        respx.post(f"{BASE}/api/generate").respond(json={"done": True})
        client2 = make_client()
        with pytest.raises(LlmError):
            await client2.generate_json("s", "p")
        await client2.aclose()


def test_LlmPort_구현():
    assert isinstance(make_client(), LlmPort)
