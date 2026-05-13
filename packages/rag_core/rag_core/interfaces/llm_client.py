"""LLMClient Protocol (ADR-002, ADR-013)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol


class LLMClient(Protocol):
    """vLLM·Ollama 등 OpenAI-compatible inference server를 추상화.

    구현체 예: VLLMClient, OllamaClient, MockLLMClient.
    """

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
        """단일 응답 생성. ADR-010 hybrid 모드는 guided_json_schema 사용."""
        ...

    async def stream(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        lora_adapter: str | None = None,
    ) -> AsyncIterator[str]:
        """SSE token-by-token streaming (ADR-013 chat_streaming)."""
        ...

    async def health(self) -> bool:
        """ADR-013 endpoint health check."""
        ...
