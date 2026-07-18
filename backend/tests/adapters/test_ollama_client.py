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


@pytest.mark.anyio
@respx.mock
async def test_response가_문자열이_아니면_LlmError():
    respx.post(f"{BASE}/api/generate").respond(json={"response": 123})
    client = make_client()
    with pytest.raises(LlmError):
        await client.generate_json("s", "p")
    await client.aclose()


def test_LlmPort_구현():
    assert isinstance(make_client(), LlmPort)


@pytest.mark.anyio
@respx.mock
async def test_마크다운_펜스는_벗겨서_반환한다():
    """클라우드 추론 경로가 format=json을 무시하고 ```json 펜스로 감싸는
    실측 동작(2026-07-18 수용 검증) 회귀 방지 — 어댑터가 벗겨서 LlmPort의
    'JSON 문자열' 계약을 지킨다."""
    respx.post(f"{BASE}/api/generate").respond(
        json={"response": '```json\n{\n  "ok": true\n}\n```', "done": True})
    client = make_client()
    out = await client.generate_json("s", "p")
    assert out == '{\n  "ok": true\n}'
    await client.aclose()


@pytest.mark.anyio
@respx.mock
async def test_펜스없는_응답은_원문_그대로다():
    # 로컬 모델 경로 — 펜스 제거가 정상 응답을 훼손하지 않아야 한다.
    # 본문 중간에 나타나는 ``` 문자열(예: JSON 값 내부)도 건드리지 않는다.
    raw = '{"summary": "코드블럭 ``` 예시", "ok": true}'
    respx.post(f"{BASE}/api/generate").respond(
        json={"response": raw, "done": True})
    client = make_client()
    out = await client.generate_json("s", "p")
    assert out == raw
    await client.aclose()


@pytest.mark.anyio
@respx.mock
async def test_백틱_4개_펜스도_대칭이면_온전히_벗긴다():
    """고정 3-백틱 매치는 4-백틱 펜스에서 잔여 백틱이 본문을 오염시켰다
    (T7 개발자 패널 실측) — 역참조로 여닫는 개수가 같을 때만 벗긴다."""
    respx.post(f"{BASE}/api/generate").respond(
        json={"response": '````json\n{"a": 1}\n````', "done": True})
    client = make_client()
    out = await client.generate_json("s", "p")
    assert out == '{"a": 1}'
    await client.aclose()


@pytest.mark.anyio
@respx.mock
async def test_비대칭_펜스는_원문_그대로다():
    # 여는 백틱 4개 + 닫는 백틱 3개 — 벗기다 만 오염 문자열 대신 원문을
    # 통과시켜 도메인 파싱이 fail-loud로 걸러내게 한다.
    raw = '````json\n{"a": 1}\n```'
    respx.post(f"{BASE}/api/generate").respond(
        json={"response": raw, "done": True})
    client = make_client()
    out = await client.generate_json("s", "p")
    assert out == raw
    await client.aclose()
