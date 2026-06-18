"""VisionLanguageClient Protocol (ADR-002, ADR-025 §4).

그림(이미지)을 이해하는 VLM 추상화. 폐쇄망 Ollama Qwen-VL이 기본 구현체.
용도(ADR-025 §4): ingestion 서술 / figure-reuse 생성 / figure validator.

text 전용 LLMClient(ADR-013)와 분리한다 — 이미지 입력이 first-class이기 때문.
"""

from __future__ import annotations

from typing import Protocol


class VisionLanguageClient(Protocol):
    """이미지 + 지시문을 받아 텍스트를 생성하는 VLM."""

    async def describe(
        self,
        *,
        image: bytes,
        prompt: str,
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> str:
        """단일 이미지에 대한 지시문(prompt)을 수행해 텍스트를 반환한다.

        ingestion 서술(그림→설명), figure-reuse 생성(그림+지시→Q&A JSON), figure
        validator(그림+문항→판정) 모두 같은 인터페이스로 호출한다.
        """
        ...

    async def health(self) -> bool:
        """VLM 서버 가용성. 비가동 시 caller는 degrade한다(ADR-025 §4)."""
        ...
