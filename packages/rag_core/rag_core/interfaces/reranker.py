"""Reranker Protocol — cross-encoder (ADR-011 §6)."""

from __future__ import annotations

from typing import Protocol

from .retriever import RetrievedChunk


class Reranker(Protocol):
    """tenant override 가능 — ADR-011 per-tenant reranker."""

    async def rerank(
        self, question: str, candidates: list[RetrievedChunk], top_k: int = 10
    ) -> list[RetrievedChunk]:
        """rerank_score를 갱신하여 정렬된 결과 반환."""
        ...
