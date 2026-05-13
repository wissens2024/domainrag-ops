"""VectorStore Protocol — Qdrant collection-per-tenant (ADR-008·011)."""

from __future__ import annotations

from typing import Any, Protocol


class VectorStore(Protocol):
    """tenant 단위 collection 관리.

    구현체:
      - QdrantVectorStore (운영)
      - InMemoryVectorStore (테스트)
    """

    async def create_collection(
        self, *, tenant_id: str, dense_dim: int, with_sparse: bool = True
    ) -> None: ...

    async def upsert_chunks(
        self,
        *,
        tenant_id: str,
        points: list[dict[str, Any]],
    ) -> None:
        """points = [{id, dense_vector, sparse_vector, payload}, ...]"""
        ...

    async def hybrid_query(
        self,
        *,
        tenant_id: str,
        dense_query: list[float],
        sparse_query: dict[int, float],
        acl_filter: dict,
        top_k: int = 50,
    ) -> list[dict[str, Any]]:
        """Qdrant DBSF fusion (ADR-011 §3)."""
        ...

    async def set_payload(
        self,
        *,
        tenant_id: str,
        chunk_ids: list[str],
        payload: dict,
    ) -> None:
        """ADR-007/012 metadata-only 갱신."""
        ...

    async def delete_collection(self, tenant_id: str) -> None: ...

    async def delete_points(
        self, *, tenant_id: str, chunk_ids: list[str]
    ) -> None:
        """ADR-007/012 hard delete — collection은 유지하고 특정 chunk만 제거."""
        ...
