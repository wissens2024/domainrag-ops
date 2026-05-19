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
        model_aliases: dict[str, str] | None = None,
    ) -> None:
        """
        Args:
            model_aliases: routing config의 logical model name(`tenant_slm`,
                `shared_llm` 등)을 vLLM이 serve하는 실 model id로 매핑. 운영자가
                env로 주입. None이면 model 이름이 그대로 vLLM에 전달.
        """
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._aliases = dict(model_aliases or {})

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def _resolve_model(self, model: str, lora_adapter: str | None) -> str:
        """vLLM 관행: LoRA 어댑터 이름을 `model` 필드로 전달하면 vLLM이
        해당 어댑터를 활성화한 base model로 추론. None이면 base model을 그대로.

        추가로 model_aliases dict로 logical name(`tenant_slm` 등)을 실 model id로
        매핑 — routing config 추상화 유지."""
        if lora_adapter:
            return lora_adapter
        return self._aliases.get(model, model)

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

    # ---------------------------------------------------------- LoRA hot-swap
    # ADR-013 §5 + ADR-019 §8 — vLLM 0.6+ extension API.
    # 운영은 KeyHub에서 weights 가져와 vLLM이 접근 가능한 로컬 경로에 둔 뒤 호출.

    async def load_lora_adapter(self, *, lora_name: str, lora_path: str) -> None:
        """vLLM에 LoRA adapter 등록 (POST /v1/load_lora_adapter).

        vLLM은 lora_path가 *자신의 로컬 파일시스템*에서 접근 가능해야 함. KeyHub
        fetch + 파일 쓰기는 caller(LoRAOrchestrator) 책임.

        성공: 응답 status<400. 실패: HTTPStatusError 또는 RuntimeError raise.
        """
        resp = await self._client.post(
            f"{self._base_url}/load_lora_adapter",
            headers=self._headers(),
            json={"lora_name": lora_name, "lora_path": lora_path},
            timeout=self._timeout,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"vllm load_lora_adapter failed: {resp.status_code} {resp.text}"
            )

    async def unload_lora_adapter(self, *, lora_name: str) -> None:
        """vLLM에서 LoRA adapter 해제 (POST /v1/unload_lora_adapter).

        idempotent — 이미 unload 된 어댑터 호출도 swallow 권장 (caller가 결정).
        """
        resp = await self._client.post(
            f"{self._base_url}/unload_lora_adapter",
            headers=self._headers(),
            json={"lora_name": lora_name},
            timeout=self._timeout,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"vllm unload_lora_adapter failed: {resp.status_code} {resp.text}"
            )
