"""InMemory mock 4종 — Protocol 계약·결정론·기본 검색 동작."""

from __future__ import annotations

import pytest

from rag_core.clients.in_memory import (
    InMemoryEmbedder,
    InMemoryLLMClient,
    InMemoryReranker,
    InMemoryVectorStore,
)
from rag_core.interfaces.retriever import RetrievedChunk


# ---------------------------- LLMClient ---------------------------- #


@pytest.mark.asyncio
async def test_llm_records_calls_and_returns_response():
    llm = InMemoryLLMClient(responses=["A", "B"])
    out1 = await llm.generate("p1", model="m", lora_adapter="lora-x")
    out2 = await llm.generate("p2", model="m")
    assert out1 == "A"
    assert out2 == "B"
    assert len(llm.calls) == 2
    assert llm.calls[0]["lora_adapter"] == "lora-x"
    assert llm.calls[0]["kind"] == "generate"


@pytest.mark.asyncio
async def test_llm_stream_yields_chunks():
    llm = InMemoryLLMClient(stream_chunks=["he", "llo"])
    out = [c async for c in llm.stream("p", model="m")]
    assert out == ["he", "llo"]
    assert llm.calls[0]["kind"] == "stream"


@pytest.mark.asyncio
async def test_llm_health_flag():
    assert await InMemoryLLMClient(healthy=True).health() is True
    assert await InMemoryLLMClient(healthy=False).health() is False


# ---------------------------- Embedder ---------------------------- #


@pytest.mark.asyncio
async def test_embedder_deterministic():
    e = InMemoryEmbedder(dense_dim=8)
    a1 = await e.embed_query("hello world")
    a2 = await e.embed_query("hello world")
    assert a1 == a2
    assert len(a1[0]) == 8


@pytest.mark.asyncio
async def test_embedder_batch_matches_query():
    e = InMemoryEmbedder(dense_dim=4)
    batch = await e.embed_batch(["a b", "c"])
    single = await e.embed_query("a b")
    assert batch[0] == single


def test_embedder_properties():
    e = InMemoryEmbedder(dense_dim=16, model_name="mock-x")
    assert e.dense_dim == 16
    assert e.model_name == "mock-x"


# ---------------------------- Reranker ---------------------------- #


def _chunk(cid: str, content: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=cid,
        doc_id="d",
        title="t",
        content=content,
        page_number=None,
        section_title=None,
        dense_score=0.0,
        sparse_score=0.0,
        fused_score=0.0,
        payload={},
    )


@pytest.mark.asyncio
async def test_reranker_jaccard_orders_candidates():
    rr = InMemoryReranker()
    cands = [
        _chunk("a", "totally unrelated text"),
        _chunk("b", "the answer is here"),
        _chunk("c", "answer here today"),
    ]
    out = await rr.rerank("the answer", cands, top_k=2)
    assert out[0].chunk_id in {"b", "c"}
    assert all(c.rerank_score is not None for c in out)
    assert len(out) == 2


# ---------------------------- VectorStore ---------------------------- #


@pytest.mark.asyncio
async def test_vector_store_create_upsert_query_filter():
    store = InMemoryVectorStore()
    await store.create_collection(tenant_id="t1", dense_dim=4)
    await store.upsert_chunks(
        tenant_id="t1",
        points=[
            {
                "id": "a",
                "dense_vector": [1.0, 0.0, 0.0, 0.0],
                "sparse_vector": {1: 1.0},
                "payload": {"approval_status": "approved", "tag": "x"},
            },
            {
                "id": "b",
                "dense_vector": [0.0, 1.0, 0.0, 0.0],
                "sparse_vector": {2: 1.0},
                "payload": {"approval_status": "draft", "tag": "x"},
            },
            {
                "id": "c",
                "dense_vector": [1.0, 0.0, 0.0, 0.0],
                "sparse_vector": {1: 1.0},
                "payload": {"approval_status": "approved", "tag": "y"},
            },
        ],
    )
    # ACL: approved + tag=x → a만 매치
    hits = await store.hybrid_query(
        tenant_id="t1",
        dense_query=[1.0, 0.0, 0.0, 0.0],
        sparse_query={1: 1.0},
        acl_filter={
            "must": [
                {"key": "approval_status", "match": {"value": "approved"}},
                {"key": "tag", "match": {"value": "x"}},
            ]
        },
        top_k=10,
    )
    assert [h["id"] for h in hits] == ["a"]


@pytest.mark.asyncio
async def test_vector_store_set_payload_updates_in_place():
    store = InMemoryVectorStore()
    await store.create_collection(tenant_id="t1", dense_dim=2)
    await store.upsert_chunks(
        tenant_id="t1",
        points=[
            {
                "id": "a",
                "dense_vector": [1.0, 0.0],
                "sparse_vector": {},
                "payload": {"approval_status": "approved"},
            }
        ],
    )
    await store.set_payload(
        tenant_id="t1",
        chunk_ids=["a"],
        payload={"approval_status": "archived"},
    )
    hits = await store.hybrid_query(
        tenant_id="t1",
        dense_query=[1.0, 0.0],
        sparse_query={},
        acl_filter={
            "must": [{"key": "approval_status", "match": {"value": "archived"}}]
        },
        top_k=5,
    )
    assert [h["id"] for h in hits] == ["a"]


@pytest.mark.asyncio
async def test_vector_store_delete_collection_removes_data():
    store = InMemoryVectorStore()
    await store.create_collection(tenant_id="t1", dense_dim=2)
    await store.delete_collection("t1")
    with pytest.raises(KeyError):
        await store.hybrid_query(
            tenant_id="t1",
            dense_query=[0.0, 0.0],
            sparse_query={},
            acl_filter={},
            top_k=5,
        )
