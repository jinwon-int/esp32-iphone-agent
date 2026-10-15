import asyncio
import base64
import io
import json

import httpx
from PIL import Image

from hidagent.llm import LLM, shrink


def fake_server(llm: LLM, wants: str) -> list[dict]:
    """Point the client at an in-memory endpoint that only accepts the `wants` token parameter."""
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if wants not in body:
            return httpx.Response(400, json={"error": {"message": f"Unsupported parameter. Use '{wants}' instead."}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "DO: wait"}}]})

    llm._http = httpx.AsyncClient(base_url="http://test/v1", transport=httpx.MockTransport(handle))
    return seen


def test_classic_servers_get_max_tokens_once():
    llm = LLM("http://test/v1", "qwen")
    seen = fake_server(llm, "max_tokens")
    assert asyncio.run(llm.chat("sys", "hi")) == "DO: wait"
    assert len(seen) == 1 and seen[0]["max_tokens"] == 600


def test_auto_switches_to_max_completion_tokens_and_remembers():
    llm = LLM("http://test/v1", "gpt")
    seen = fake_server(llm, "max_completion_tokens")
    asyncio.run(llm.chat("sys", "hi"))
    asyncio.run(llm.chat("sys", "again"))
    assert [("max_tokens" in b, "max_completion_tokens" in b) for b in seen] == [(True, False), (False, True),
                                                                                 (False, True)]


def test_explicit_token_param_is_used_directly():
    llm = LLM("http://test/v1", "gpt", token_param="max_completion_tokens")
    seen = fake_server(llm, "max_completion_tokens")
    asyncio.run(llm.chat("sys", "hi"))
    assert len(seen) == 1


def test_extra_body_and_image_are_sent():
    llm = LLM("http://test/v1", "gpt", extra={"reasoning_effort": "none"})
    seen = fake_server(llm, "max_tokens")
    asyncio.run(llm.chat("sys", "hi", image=b"\xff\xd8jpeg"))
    body = seen[0]
    assert body["reasoning_effort"] == "none"
    assert body["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def jpeg(w: int, h: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(out, "JPEG")
    return out.getvalue()


def sent_size(body: dict) -> tuple[int, int]:
    url = body["messages"][1]["content"][0]["image_url"]["url"]
    return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).size


def test_image_is_sent_as_received_by_default():
    llm = LLM("http://test/v1", "gpt")
    seen = fake_server(llm, "max_tokens")
    asyncio.run(llm.chat("sys", "hi", image=jpeg(590, 1280)))
    assert sent_size(seen[0]) == (590, 1280)


def test_image_max_side_shrinks_keeping_the_aspect():
    llm = LLM("http://test/v1", "gpt", image_max=640)
    seen = fake_server(llm, "max_tokens")
    asyncio.run(llm.chat("sys", "hi", image=jpeg(590, 1280)))
    w, h = sent_size(seen[0])
    assert h == 640 and abs(w - 295) <= 1


def test_shrink_leaves_small_or_unreadable_frames_alone():
    small = jpeg(300, 600)
    assert shrink(small, 640) is small
    assert shrink(b"\xff\xd8jpeg", 640) == b"\xff\xd8jpeg"
    assert shrink(small, 0) is small


def test_image_max_side_from_env_role_overrides_shared(monkeypatch):
    monkeypatch.setenv("LLM_IMAGE_MAX_SIDE", "1024")
    monkeypatch.setenv("ACTOR_IMAGE_MAX_SIDE", "512")
    monkeypatch.delenv("PLANNER_IMAGE_MAX_SIDE", raising=False)
    assert LLM.from_env("ACTOR").image_max == 512
    assert LLM.from_env("PLANNER").image_max == 1024
    monkeypatch.delenv("LLM_IMAGE_MAX_SIDE")
    assert LLM.from_env("PLANNER").image_max == 0
