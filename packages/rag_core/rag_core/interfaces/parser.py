"""DocumentParser Protocol (ADR-002)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ParsedPage:
    page_number: int
    text: str
    section_title: str | None = None
    heading_path: list[str] | None = None


class DocumentParser(Protocol):
    """파일 형식별 구현체 — PdfParser, DocxParser, TxtParser, HwpParser, ..."""

    parser_version: str

    async def parse(self, file_path: str) -> list[ParsedPage]: ...

    @classmethod
    def supported_extensions(cls) -> list[str]: ...
