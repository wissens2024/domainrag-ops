"""ADR-012 §3 — archived chunk가 retrieval에서 제외되는지 검증.

InMemoryVectorStore의 must_not 절 처리 + RetrievalService 통합 + ACL builder 정합.
archival_worker가 set_payload({"archived": True})한 chunk가 hybrid_query에서 자동 누락
되어야 한다.
"""

from __future__ import annotations

from rag_core.clients.in_memory import (
    InMemoryEmbedder,
    InMemoryReranker,
    InMemoryVectorStore,
)
from rag_core.services.acl_builder import build_qdrant_acl_filter
from rag_core.services.retrieval_service import RetrievalService


def _payload(chunk_id: str, extra: dict | None = None) -> dict:
    base = {
        "approval_status": "approved",
        "security_level": "internal",
        "acl": ["group:security"],
        "doc_id": "d1",
        "content": f"content {chunk_id}",
    }
    if extra:
        base.update(extra)
    return base


async def _populate(store: InMemoryVectorStore, embedder: InMemoryEmbedder):
    await store.create_collection(tenant_id="t1", dense_dim=64)
    points = []
    for cid, archived in [("c1", False), ("c2", True)]:
        d, s = await embedder.embed_query(f"chunk {cid}")
        points.append(
            {
                "id": cid,
                "dense_vector": d,
                "sparse_vector": s,
                "payload": _payload(cid, {"archived": True} if archived else None),
            }
        )
    await store.upsert_chunks(tenant_id="t1", points=points)


async def test_must_not_excludes_archived_chunk():
    """InMemoryVectorStore.hybrid_query는 must_not의 모든 조건을 만족하는 chunk를 제외."""
    embedder = InMemoryEmbedder(dense_dim=64)
    store = InMemoryVectorStore()
    await _populate(store, embedder)

    acl = build_qdrant_acl_filter(
        user_id="u1",
        clearance="internal",
        department="security",
        domain_groups=["group:security"],
    )
    d, s = await embedder.embed_query("chunk")
    hits = await store.hybrid_query(
        tenant_id="t1", dense_query=d, sparse_query=s, acl_filter=acl, top_k=10
    )
    ids = {h["id"] for h in hits}
    assert "c1" in ids
    assert "c2" not in ids  # archived=true → must_not으로 제외


async def test_exclude_archived_false_includes_archived():
    """exclude_archived=False로 빌드된 filter는 archived chunk도 결과에 포함."""
    embedder = InMemoryEmbedder(dense_dim=64)
    store = InMemoryVectorStore()
    await _populate(store, embedder)

    acl = build_qdrant_acl_filter(
        user_id="u1",
        clearance="internal",
        department="security",
        domain_groups=["group:security"],
        exclude_archived=False,
    )
    d, s = await embedder.embed_query("chunk")
    hits = await store.hybrid_query(
        tenant_id="t1", dense_query=d, sparse_query=s, acl_filter=acl, top_k=10
    )
    ids = {h["id"] for h in hits}
    assert {"c1", "c2"}.issubset(ids)


async def test_retrieval_service_respects_archived_filter():
    """RetrievalService.retrieve도 acl_filter를 그대로 전달 — archived 제외 효과 유지."""
    embedder = InMemoryEmbedder(dense_dim=64)
    store = InMemoryVectorStore()
    await _populate(store, embedder)
    reranker = InMemoryReranker()
    retrieval = RetrievalService(
        embedder=embedder, vector_store=store, reranker=reranker
    )
    acl = build_qdrant_acl_filter(
        user_id="u1",
        clearance="internal",
        department="security",
        domain_groups=["group:security"],
    )
    result = await retrieval.retrieve(
        tenant_id="t1", question="chunk", acl_filter=acl
    )
    fused_ids = {c.chunk_id for c in result.fused}
    assert "c1" in fused_ids
    assert "c2" not in fused_ids
