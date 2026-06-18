"""OllamaVisionClient — base64 이미지 전송 + 응답 파싱 + health (ADR-025 §4)."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from rag_core.clients.ollama_vision_client import OllamaVisionClient


@pytest.mark.asyncio
async def test_describe_sends_base64_image_and_parses_response(mock_async_client):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"response": "루트 a, 좌 b, 우 c인 이진트리"})

    async with mock_async_client(handler) as client:
        vlm = OllamaVisionClient(base_url="http://ollama:11434", model="qwen2.5vl:7b", client=client)
        out = await vlm.describe(image=b"\x89PNG_fake", prompt="이 그림을 설명하라", max_tokens=256)

    assert out == "루트 a, 좌 b, 우 c인 이진트리"
    assert captured["path"] == "/api/generate"
    body = captured["body"]
    assert body["model"] == "qwen2.5vl:7b"
    assert body["stream"] is False
    assert body["prompt"] == "이 그림을 설명하라"
    assert body["options"]["num_predict"] == 256
    # 이미지가 base64로 실려야 함
    assert body["images"] == [base64.b64encode(b"\x89PNG_fake").decode("ascii")]


@pytest.mark.asyncio
async def test_describe_model_override(mock_async_client):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"response": "ok"})

    async with mock_async_client(handler) as client:
        vlm = OllamaVisionClient(base_url="http://ollama:11434", model="qwen2.5vl:7b", client=client)
        await vlm.describe(image=b"x", prompt="p", model="qwen2.5vl:3b")

    assert captured["body"]["model"] == "qwen2.5vl:3b"  # 호출 override 우선


@pytest.mark.asyncio
async def test_describe_empty_response_returns_empty_string(mock_async_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})  # response 키 없음

    async with mock_async_client(handler) as client:
        vlm = OllamaVisionClient(base_url="http://ollama:11434", client=client)
        out = await vlm.describe(image=b"x", prompt="p")
    assert out == ""


@pytest.mark.asyncio
async def test_health_true_on_tags_200(mock_async_client):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": []})

    async with mock_async_client(handler) as client:
        vlm = OllamaVisionClient(base_url="http://ollama:11434", client=client)
        assert await vlm.health() is True


@pytest.mark.asyncio
async def test_health_false_on_error(mock_async_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with mock_async_client(handler) as client:
        vlm = OllamaVisionClient(base_url="http://ollama:11434", client=client)
        # 500은 raise_for_status 안 함(health는 status_code만 봄) → False
        assert await vlm.health() is False
