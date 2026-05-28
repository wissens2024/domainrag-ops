"""QdrantVectorStore — collection-per-tenant + named dense/sparse + DBSF fusion.

ADR-008·011 정합:
  - collection name pattern: chunks_{domain_id}
  - named vectors: "dense" (cosine) + "sparse"
  - hybrid_query: prefetch dense + sparse → DBSF fusion (한 호출)
  - acl_filter는 Qdrant payload filter dict (caller가 build_acl_filter로 생성)
"""

from __future__ import annotations

from typing import Any

from qdrant_client import AsyncQdrantClient, models

from ..interfaces.retriever import RetrievedChunk


def _collection_name(domain_id: str) -> str:
    return f"chunks_{domain_id}"


def _to_filter(acl_filter: dict | None) -> models.Filter | None:
    if not acl_filter:
        return None
    return models.Filter.model_validate(acl_filter)


def _to_sparse(sparse: dict[int, float]) -> models.SparseVector:
    indices = list(sparse.keys())
    values = [sparse[i] for i in indices]
    return models.SparseVector(indices=indices, values=values)


class QdrantVectorStore:
    """AsyncQdrantClient 기반. 외부에서 client 주입 가능 (테스트는 :memory: 또는 mock).

    Args:
        client: qdrant-client AsyncQdrantClient 인스턴스 (DI)
        distance: dense vector distance metric (기본 COSINE)
    """

    def __init__(
        self,
        *,
        client: AsyncQdrantClient,
        distance: models.Distance = models.Distance.COSINE,
    ) -> None:
        self._client = client
        self._distance = distance

    async def create_collection(
        self, *, domain_id: str, dense_dim: int, with_sparse: bool = True
    ) -> None:
        vectors_config = {
            "dense": models.VectorParams(size=dense_dim, distance=self._distance),
        }
        sparse_vectors_config = (
            {"sparse": models.SparseVectorParams(index=models.SparseIndexParams())}
            if with_sparse
            else None
        )
        await self._client.create_collection(
            collection_name=_collection_name(domain_id),
            vectors_config=vectors_config,
            sparse_vectors_config=sparse_vectors_config,
        )

    async def upsert_chunks(
        self,
        *,
        domain_id: str,
        points: list[dict[str, Any]],
    ) -> None:
        """points = [{id, dense_vector, sparse_vector, payload}, ...]

        sparse_vector는 dict[int, float] 형식.
        """
        qdrant_points: list[models.PointStruct] = []
        for p in points:
            vector: dict[str, Any] = {"dense": p["dense_vector"]}
            sparse = p.get("sparse_vector")
            if sparse:
                vector["sparse"] = _to_sparse(sparse)
            qdrant_points.append(
                models.PointStruct(
                    id=p["id"],
                    vector=vector,
                    payload=p.get("payload") or {},
                )
            )
        await self._client.upsert(
            collection_name=_collection_name(domain_id),
            points=qdrant_points,
            wait=True,
        )

    async def hybrid_query(
        self,
        *,
        domain_id: str,
        dense_query: list[float],
        sparse_query: dict[int, float],
        acl_filter: dict,
        top_k: int = 50,
    ) -> list[dict[str, Any]]:
        """ADR-011 §3 Qdrant DBSF fusion. prefetch limit은 top_k와 동일하게 둠
        (호출측에서 dense/sparse top_k를 따로 제어할 수 있도록 추후 확장 여지)."""
        qfilter = _to_filter(acl_filter)
        result = await self._client.query_points(
            collection_name=_collection_name(domain_id),
            prefetch=[
                models.Prefetch(
                    query=dense_query,
                    using="dense",
                    limit=top_k,
                    filter=qfilter,
                ),
                models.Prefetch(
                    query=_to_sparse(sparse_query),
                    using="sparse",
                    limit=top_k,
                    filter=qfilter,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.DBSF),
            query_filter=qfilter,
            limit=top_k,
            with_payload=True,
        )
        return [
            {
                "id": str(p.id),
                "score": float(p.score) if p.score is not None else 0.0,
                "payload": p.payload or {},
            }
            for p in result.points
        ]

    async def set_payload(
        self,
        *,
        domain_id: str,
        chunk_ids: list[str],
        payload: dict,
    ) -> None:
        """ADR-007/012 metadata-only 갱신 (vector는 보존, payload만 partial update)."""
        await self._client.set_payload(
            collection_name=_collection_name(domain_id),
            payload=payload,
            points=chunk_ids,
            wait=True,
        )

    async def delete_collection(self, domain_id: str) -> None:
        await self._client.delete_collection(_collection_name(domain_id))

    async def delete_points(
        self, *, domain_id: str, chunk_ids: list[str]
    ) -> None:
        """ADR-007/012 hard delete — collection은 유지하고 특정 chunk만 제거."""
        if not chunk_ids:
            return
        await self._client.delete(
            collection_name=_collection_name(domain_id),
            points_selector=list(chunk_ids),
            wait=True,
        )


def hits_to_retrieved_chunks(hits: list[dict[str, Any]]) -> list[RetrievedChunk]:
    """hybrid_query 결과를 RetrievedChunk로 변환하는 헬퍼.

    DBSF fusion 결과의 score는 fused_score로 사용. dense/sparse 개별 score는
    fusion 단계에서 정규화되어 별도 필드로 반환되지 않으므로 0.0으로 둔다
    (필요 시 caller가 prefetch 결과를 별도로 보존하는 변형 호출을 만들 것).
    """
    out: list[RetrievedChunk] = []
    for h in hits:
        payload = h.get("payload") or {}
        out.append(
            RetrievedChunk(
                chunk_id=str(h.get("id")),
                doc_id=str(payload.get("doc_id", "")),
                title=str(payload.get("title", "")),
                content=str(payload.get("content", "")),
                page_number=payload.get("page_number"),
                section_title=payload.get("section_title"),
                dense_score=0.0,
                sparse_score=0.0,
                fused_score=float(h.get("score", 0.0)),
                payload=payload,
            )
        )
    return out
