"""RetrievalService — Embedder · VectorStore · Reranker를 묶는 운영 서비스.

ADR-011 §9·§10 + §12 (reranker bypass) 정합:
  embed → hybrid_query (DBSF) → (옵션) rerank → context truncation
"""

from __future__ import annotations

from dataclasses import dataclass

from ..clients.qdrant_store import hits_to_retrieved_chunks
from ..interfaces.embedder import Embedder
from ..interfaces.reranker import Reranker
from ..interfaces.retriever import RetrievedChunk
from ..interfaces.vector_store import VectorStore


@dataclass(frozen=True)
class RetrievalConfig:
    """ADR-011 §3·§11 top_k 단계와 reranker bypass 옵션."""

    fused_top_k: int = 50
    rerank_top_k: int = 10
    context_top_k: int = 5
    reranker_bypass: bool = False


@dataclass
class RetrievalResult:
    fused: list[RetrievedChunk]       # DBSF fusion 직후 (rerank 전)
    reranked: list[RetrievedChunk]    # rerank 적용 (bypass 시 fused 그대로)
    contexts: list[RetrievedChunk]    # 최종 LLM에 전달될 context (top context_top_k)


class RetrievalService:
    """Tenant-scoped retrieval orchestrator.

    config는 호출 시 주입(요청별 override 가능). embedder·vector_store·reranker는
    인스턴스 생성 시 주입된다. tenant별 reranker override가 필요해지면 RerankerService를
    별도 도입해 본 service에서 lookup하는 형태로 확장한다.
    """

    def __init__(
        self,
        *,
        embedder: Embedder,
        vector_store: VectorStore,
        reranker: Reranker | None = None,
    ) -> None:
        self._embedder = embedder
        self._vector_store = vector_store
        self._reranker = reranker

    @property
    def embedder(self) -> Embedder:
        """IndexingService와 임베더를 공유할 수 있게 노출 (ADR-011 §10)."""
        return self._embedder

    @property
    def vector_store(self) -> VectorStore:
        """IndexingService.upsert_to_qdrant와 vector store를 공유하기 위한 access."""
        return self._vector_store

    async def retrieve(
        self,
        *,
        tenant_id: str,
        question: str,
        acl_filter: dict,
        config: RetrievalConfig | None = None,
    ) -> RetrievalResult:
        cfg = config or RetrievalConfig()

        dense_query, sparse_query = await self._embedder.embed_query(question)
        hits = await self._vector_store.hybrid_query(
            tenant_id=tenant_id,
            dense_query=dense_query,
            sparse_query=sparse_query,
            acl_filter=acl_filter,
            top_k=cfg.fused_top_k,
        )
        fused = hits_to_retrieved_chunks(hits)

        if cfg.reranker_bypass or self._reranker is None:
            reranked = fused[: cfg.rerank_top_k]
        else:
            reranked = await self._reranker.rerank(
                question, fused, top_k=cfg.rerank_top_k
            )

        contexts = reranked[: cfg.context_top_k]
        return RetrievalResult(fused=fused, reranked=reranked, contexts=contexts)
