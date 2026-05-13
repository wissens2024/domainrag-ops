"""Chunker Protocol (ADR-002, ADR-007/012 chunk_id 형식)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .parser import ParsedPage


@dataclass
class Chunk:
    chunk_id: str  # format: <doc_id>:<doc_version>:<parser_version>:chunk:<index>
    chunk_index: int
    content: str
    page_number: int | None
    section_title: str | None
    heading_path: list[str]
    start_char: int
    end_char: int
    token_count: int


class Chunker(Protocol):
    """semantic-v1 / fixed-window-v1 등 strategy별 구현체.

    chunk_strategy 식별자 (chunks 테이블 컬럼)는 본 구현체가 책임.
    """

    chunk_strategy: str

    async def chunk(
        self,
        *,
        doc_id: str,
        doc_version: str,
        parser_version: str,
        pages: list[ParsedPage],
    ) -> list[Chunk]: ...
