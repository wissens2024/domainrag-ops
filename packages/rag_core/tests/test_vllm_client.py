"""VllmLLMClient — generate / stream / health / lora_adapter / guided_json."""

from __future__ import annotations

import json

import httpx
import pytest

from rag_core.clients.vllm_client import VllmLLMClient


@pytest.mark.asyncio
async def test_generate_basic(mock_async_client, json_response):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode())
        return json_response(
            {
                "choices": [{"message": {"content": "hello"}}],
            }
        )

    async with mock_async_client(handler) as client:
        llm = VllmLLMClient(base_url="http://vllm/v1", client=client)
        out = await llm.generate(
            "say hi", model="qwen-7b", max_tokens=64, temperature=0.0
        )
    assert out == "hello"
    assert seen["url"].endswith("/v1/chat/completions")
    body = seen["body"]
    assert body["model"] == "qwen-7b"
    assert body["max_tokens"] == 64
    assert body["temperature"] == 0.0
    assert body["stream"] is False
    assert body["messages"][0]["content"] == "say hi"
    assert "guided_json" not in body


@pytest.mark.asyncio
async def test_generate_with_lora_routes_to_adapter_model(mock_async_client, json_response):
    """lora_adapter가 있으면 그 이름이 `model` 필드로 전달된다 (vLLM multi-LoRA 관행)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return json_response({"choices": [{"message": {"content": "ok"}}]})

    async with mock_async_client(handler) as client:
        llm = VllmLLMClient(base_url="http://vllm/v1", client=client)
        await llm.generate(
            "q", model="qwen-7b", lora_adapter="security-policy-v1"
        )
    assert seen["body"]["model"] == "security-policy-v1"


@pytest.mark.asyncio
async def test_generate_with_guided_json(mock_async_client, json_response):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return json_response(
            {"choices": [{"message": {"content": '{"a":1}'}}]}
        )

    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    async with mock_async_client(handler) as client:
        llm = VllmLLMClient(base_url="http://vllm/v1", client=client)
        out = await llm.generate("x", model="qwen-7b", guided_json_schema=schema)
    assert out == '{"a":1}'
    assert seen["body"]["guided_json"] == schema


@pytest.mark.asyncio
async def test_stream_yields_content_deltas(mock_async_client):
    sse_body = (
        b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
        b'data: [DONE]\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse_body,
            headers={"Content-Type": "text/event-stream"},
        )

    async with mock_async_client(handler) as client:
        llm = VllmLLMClient(base_url="http://vllm/v1", client=client)
        chunks = [c async for c in llm.stream("hi", model="qwen-7b")]
    assert chunks == ["he", "llo"]


@pytest.mark.asyncio
async def test_stream_skips_malformed_lines(mock_async_client):
    sse_body = (
        b': comment line\n\n'
        b'data: not-json\n\n'
        b'data: {"choices":[{"delta":{}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
        b'data: [DONE]\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse_body,
            headers={"Content-Type": "text/event-stream"},
        )

    async with mock_async_client(handler) as client:
        llm = VllmLLMClient(base_url="http://vllm/v1", client=client)
        chunks = [c async for c in llm.stream("hi", model="qwen-7b")]
    assert chunks == ["x"]


@pytest.mark.asyncio
async def test_health_true_when_models_ok(mock_async_client, json_response):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            return httpx.Response(404)
        return json_response({"data": []})

    async with mock_async_client(handler) as client:
        llm = VllmLLMClient(base_url="http://vllm/v1", client=client)
        assert await llm.health() is True


@pytest.mark.asyncio
async def test_health_false_when_all_endpoints_fail(mock_async_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with mock_async_client(handler) as client:
        llm = VllmLLMClient(base_url="http://vllm/v1", client=client)
        assert await llm.health() is False
