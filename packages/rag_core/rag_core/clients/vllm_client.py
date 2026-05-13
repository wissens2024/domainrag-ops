"""VllmLLMClient — vLLM OpenAI-compatible /v1/chat/completions 클라이언트.

ADR-013 §4·§5·§6·§7 정합:
  - generate: sync, optional guided_json_schema (vLLM extension)
  - stream:   SSE token-by-token (chat_streaming 모드)
  - lora_adapter는 vLLM 관행대로 `model` 필드에 LoRA 이름을 전달
    (vLLM이 --enable-lora + --lora-modules로 등록된 어댑터를 자동 라우팅)
  - health:   GET /health (없으면 GET /v1/models로 fallback)

guided_json은 vLLM 전용 확장으로 OpenAI 표준 외 필드. 우리는 raw httpx 호출로 처리.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx


class VllmLLMClient:
    """OpenAI-compatible vLLM 엔드포인트 클라이언트.

    Args:
        base_url: 예) "http://vllm-tenant:8000/v1"  (끝의 /v1 포함)
        timeout_seconds: 호출 timeout (ADR-013 timeout_seconds)
        api_key: vLLM은 보통 무인증이지만 reverse proxy 인증을 위해 옵션 제공
        client: 외부에서 httpx.AsyncClient 주입 가능 (테스트용)
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 30.0,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    @staticmethod
    def _resolve_model(model: str, lora_adapter: str | None) -> str:
        """vLLM 관행: LoRA 어댑터 이름을 `model` 필드로 전달하면 vLLM이
        해당 어댑터를 활성화한 base model로 추론. None이면 base model을 그대로."""
        return lora_adapter if lora_adapter else model

    async def generate(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        guided_json_schema: dict | None = None,
        lora_adapter: str | None = None,
    ) -> str:
        body: dict = {
            "model": self._resolve_model(model, lora_adapter),
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if guided_json_schema is not None:
            # vLLM extension: structured output via JSON schema
            body["guided_json"] = guided_json_schema

        resp = await self._client.post(
            f"{self._base_url}/chat/completions",
            headers=self._headers(),
            json=body,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"unexpected vLLM response shape: {data}") from e

    async def stream(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        lora_adapter: str | None = None,
    ) -> AsyncIterator[str]:
        body: dict = {
            "model": self._resolve_model(model, lora_adapter),
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        async with self._client.stream(
            "POST",
            f"{self._base_url}/chat/completions",
            headers=self._headers(),
            json=body,
            timeout=self._timeout,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    return
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                try:
                    delta = chunk["choices"][0].get("delta", {})
                except (KeyError, IndexError, TypeError):
                    continue
                content = delta.get("content")
                if content:
                    yield content

    async def health(self) -> bool:
        # vLLM 0.6+ has /health; if absent (404 등), fall back to /models which always exists.
        for path in ("/health", "/models"):
            try:
                resp = await self._client.get(
                    f"{self._base_url}{path}",
                    headers=self._headers(),
                    timeout=min(self._timeout, 5.0),
                )
                if resp.status_code < 400:
                    return True
            except httpx.HTTPError:
                continue
        return False
