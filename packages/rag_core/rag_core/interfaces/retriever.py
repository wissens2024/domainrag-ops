"""Retriever Protocol — Hybrid Dense+Sparse (ADR-011)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class RetrievedChunk:
    chunk_id: str
    doc_id: str
    title: str
    content: str
    page_number: int | None
    section_title: str | None
    dense_score: float
    sparse_score: float
    fused_score: float
    payload: dict[str, Any]
    rerank_score: float | None = None  # ADR-011 §10 reranker가 갱신


class Retriever(Protocol):
    """Tenant-scoped retriever — collection-per-tenant (ADR-008)."""

    async def retrieve(
        self,
        *,
        tenant_id: str,
        question: str,
        acl_filter: dict,            # ADR-004 + ADR-018에서 빌드
        top_k: int = 50,
    ) -> list[RetrievedChunk]:
        """ADR-011 §3 Qdrant DBSF fusion으로 dense+sparse → fused 반환."""
        ...
