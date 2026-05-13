"""PIIDetector Protocol (ADR-020)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass
class PIIFinding:
    category: str  # rrn | credit_card | api_key | phone | email | ...
    severity: Literal["low", "medium", "high"]
    position: tuple[int, int]  # (start, end) char index
    matched_text: str
    masked_form: str


class PIIDetector(Protocol):
    """4-layer 처리에 모두 사용되는 단일 인터페이스.

    구현체:
      - RegexPIIDetector (정규식, ADR-020 기본)
      - NLIPIIDetector (Phase 후속)
      - MockPIIDetector (테스트)
    """

    def scan(self, text: str) -> list[PIIFinding]:
        """탐지만. 마스킹은 mask()."""
        ...

    def mask(self, text: str) -> tuple[str, list[PIIFinding]]:
        """마스킹된 텍스트와 발견 목록 반환."""
        ...
