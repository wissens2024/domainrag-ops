"""OllamaVisionClient — 폐쇄망 Ollama Qwen-VL VisionLanguageClient 구현 (ADR-025 §4).

Ollama native API `/api/generate`에 base64 이미지를 실어 호출한다(스트리밍 off).
모델은 configs로 주입(예 qwen2.5vl:7b). 외부 API 금지 원칙(원칙 12) 준수 — 174 GPU0
Ollama 내부만 사용(authfusion·WiSentinel과 공유, ADR-019).
"""

from __future__ import annotations

import base64

import httpx


class OllamaVisionClient:
    """Ollama Qwen-VL 클라이언트.

    Args:
        base_url: 예) "http://174.local:11434" (Ollama 루트, /api 미포함)
        model: 기본 VLM 모델 id (예 "qwen2.5vl:7b"). 호출 시 override 가능.
        timeout_seconds: VLM은 느리므로 넉넉히 (기본 120s).
        client: 외부 httpx.AsyncClient 주입(테스트용).
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str = "qwen2.5vl:7b",
        timeout_seconds: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def model(self) -> str:
        return self._model

    async def describe(
        self,
        *,
        image: bytes,
        prompt: str,
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> str:
        b64 = base64.b64encode(image).decode("ascii")
        payload = {
            "model": model or self._model,
            "prompt": prompt,
            "images": [b64],
            "stream": False,
            "options": {"num_predict": max_tokens, "temperature": temperature},
        }
        resp = await self._client.post(
            f"{self._base_url}/api/generate", json=payload, timeout=self._timeout
        )
        resp.raise_for_status()
        data = resp.json()
        return str(data.get("response", "") or "")

    async def health(self) -> bool:
        try:
            resp = await self._client.get(f"{self._base_url}/api/tags", timeout=10.0)
            return resp.status_code == 200
        except Exception:  # noqa: BLE001 — 비가동이면 degrade
            return False
