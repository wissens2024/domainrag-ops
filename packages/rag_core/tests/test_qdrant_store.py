"""QdrantVectorStore — AsyncQdrantClient mock으로 호출 인자·변환 검증."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from qdrant_client import models

from rag_core.clients.qdrant_store import (
    QdrantVectorStore,
    hits_to_retrieved_chunks,
)


@pytest.fixture
def mock_qdrant() -> AsyncMock:
    client = AsyncMock()
    client.create_collection = AsyncMock()
    client.upsert = AsyncMock()
    client.query_points = AsyncMock()
    client.set_payload = AsyncMock()
    client.delete_collection = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_create_collection_sets_named_vectors(mock_qdrant):
    store = QdrantVectorStore(client=mock_qdrant)
    await store.create_collection(domain_id="security", dense_dim=1024)
    mock_qdrant.create_collection.assert_awaited_once()
    kwargs = mock_qdrant.create_collection.await_args.kwargs
    assert kwargs["collection_name"] == "chunks_security"
    assert "dense" in kwargs["vectors_config"]
    assert kwargs["vectors_config"]["dense"].size == 1024
    assert kwargs["vectors_config"]["dense"].distance == models.Distance.COSINE
    assert "sparse" in kwargs["sparse_vectors_config"]


@pytest.mark.asyncio
async def test_create_collection_without_sparse(mock_qdrant):
    store = QdrantVectorStore(client=mock_qdrant)
    await store.create_collection(
        domain_id="legal", dense_dim=768, with_sparse=False
    )
    kwargs = mock_qdrant.create_collection.await_args.kwargs
    assert kwargs["sparse_vectors_config"] is None


@pytest.mark.asyncio
async def test_upsert_chunks_builds_point_structs(mock_qdrant):
    store = QdrantVectorStore(client=mock_qdrant)
    await store.upsert_chunks(
        domain_id="security",
        points=[
            {
                "id": "c1",
                "dense_vector": [0.1, 0.2],
                "sparse_vector": {3: 0.5, 7: 0.2},
                "payload": {"doc_id": "d1", "title": "t"},
            }
        ],
    )
    kwargs = mock_qdrant.upsert.await_args.kwargs
    assert kwargs["collection_name"] == "chunks_security"
    pts = kwargs["points"]
    assert len(pts) == 1
    p = pts[0]
    assert p.id == "c1"
    assert p.vector["dense"] == [0.1, 0.2]
    sparse = p.vector["sparse"]
    assert sorted(zip(sparse.indices, sparse.values)) == [(3, 0.5), (7, 0.2)]
    assert p.payload == {"doc_id": "d1", "title": "t"}


@pytest.mark.asyncio
async def test_upsert_omits_sparse_when_absent(mock_qdrant):
    store = QdrantVectorStore(client=mock_qdrant)
    await store.upsert_chunks(
        domain_id="security",
        points=[{"id": "c1", "dense_vector": [0.1], "payload": {}}],
    )
    pts = mock_qdrant.upsert.await_args.kwargs["points"]
    assert "sparse" not in pts[0].vector


@pytest.mark.asyncio
async def test_hybrid_query_passes_dbsf_and_filter(mock_qdrant):
    mock_qdrant.query_points.return_value = SimpleNamespace(
        points=[
            SimpleNamespace(id="c1", score=0.9, payload={"doc_id": "d1"}),
            SimpleNamespace(id="c2", score=0.8, payload={"doc_id": "d2"}),
        ]
    )
    store = QdrantVectorStore(client=mock_qdrant)
    acl = {
        "must": [
            {"key": "approval_status", "match": {"value": "approved"}},
            {"key": "domain_groups", "match": {"any": ["security"]}},
        ]
    }
    hits = await store.hybrid_query(
        domain_id="security",
        dense_query=[0.1, 0.2, 0.3],
        sparse_query={4: 0.5},
        acl_filter=acl,
        top_k=10,
    )
    assert hits == [
        {"id": "c1", "score": 0.9, "payload": {"doc_id": "d1"}},
        {"id": "c2", "score": 0.8, "payload": {"doc_id": "d2"}},
    ]
    kwargs = mock_qdrant.query_points.await_args.kwargs
    assert kwargs["collection_name"] == "chunks_security"
    assert kwargs["limit"] == 10
    assert isinstance(kwargs["query"], models.FusionQuery)
    assert kwargs["query"].fusion == models.Fusion.DBSF
    assert isinstance(kwargs["query_filter"], models.Filter)
    assert len(kwargs["prefetch"]) == 2
    dense_pf, sparse_pf = kwargs["prefetch"]
    assert dense_pf.using == "dense"
    assert dense_pf.query == [0.1, 0.2, 0.3]
    assert sparse_pf.using == "sparse"
    assert isinstance(sparse_pf.query, models.SparseVector)


@pytest.mark.asyncio
async def test_set_payload_targets_collection(mock_qdrant):
    store = QdrantVectorStore(client=mock_qdrant)
    await store.set_payload(
        domain_id="security",
        chunk_ids=["c1", "c2"],
        payload={"approval_status": "archived"},
    )
    kwargs = mock_qdrant.set_payload.await_args.kwargs
    assert kwargs["collection_name"] == "chunks_security"
    assert kwargs["points"] == ["c1", "c2"]
    assert kwargs["payload"] == {"approval_status": "archived"}


@pytest.mark.asyncio
async def test_delete_collection(mock_qdrant):
    store = QdrantVectorStore(client=mock_qdrant)
    await store.delete_collection("security")
    mock_qdrant.delete_collection.assert_awaited_once_with("chunks_security")


def test_hits_to_retrieved_chunks_maps_payload():
    hits = [
        {
            "id": "c1",
            "score": 0.7,
            "payload": {
                "doc_id": "d1",
                "title": "T",
                "content": "body",
                "page_number": 3,
                "section_title": "S",
            },
        }
    ]
    out = hits_to_retrieved_chunks(hits)
    assert len(out) == 1
    rc = out[0]
    assert rc.chunk_id == "c1"
    assert rc.fused_score == 0.7
    assert rc.title == "T"
    assert rc.content == "body"
    assert rc.page_number == 3
    assert rc.section_title == "S"
    assert rc.rerank_score is None
