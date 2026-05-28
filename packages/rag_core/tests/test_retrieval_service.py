"""RetrievalService — embed → hybrid_query → rerank → context truncation."""

from __future__ import annotations

import pytest

from rag_core.clients.in_memory import (
    InMemoryEmbedder,
    InMemoryReranker,
    InMemoryVectorStore,
)
from rag_core.services.acl_builder import build_qdrant_acl_filter
from rag_core.services.retrieval_service import RetrievalConfig, RetrievalService


@pytest.fixture
async def populated_store():
    store = InMemoryVectorStore()
    embedder = InMemoryEmbedder(dense_dim=8)
    await store.create_collection(domain_id="security", dense_dim=8)

    docs = [
        ("c1", "패스워드 정책 12자 이상 복합 문자",
            {"approval_status": "approved", "security_level": "internal",
             "acl": ["group:security", "dept:security"], "doc_id": "d1",
             "title": "Password Policy", "page_number": 1, "section_title": "S1",
             "content": "패스워드 정책 12자 이상 복합 문자"}),
        ("c2", "VPN 접속 절차 토큰 발급",
            {"approval_status": "approved", "security_level": "internal",
             "acl": ["group:security"], "doc_id": "d2",
             "title": "VPN Access", "page_number": 5, "section_title": "S2",
             "content": "VPN 접속 절차 토큰 발급"}),
        ("c3", "사내 보안 사고 대응 절차",
            {"approval_status": "approved", "security_level": "confidential",
             "acl": ["group:security"], "doc_id": "d3",
             "title": "Incident Response", "page_number": 10, "section_title": "S3",
             "content": "사내 보안 사고 대응 절차"}),
        ("c4", "인사 평가 가이드 (보안과 무관)",
            {"approval_status": "approved", "security_level": "internal",
             "acl": ["group:hr"], "doc_id": "d4",
             "title": "HR Eval", "page_number": 2, "section_title": "S4",
             "content": "인사 평가 가이드"}),
        ("c5", "DRAFT 문서 — 미승인",
            {"approval_status": "draft", "security_level": "internal",
             "acl": ["group:security"], "doc_id": "d5",
             "title": "Draft", "page_number": 3, "section_title": "S5",
             "content": "DRAFT"}),
    ]
    points = []
    for cid, text, payload in docs:
        dense, sparse = await embedder.embed_query(text)
        points.append(
            {
                "id": cid,
                "dense_vector": dense,
                "sparse_vector": sparse,
                "payload": payload,
            }
        )
    await store.upsert_chunks(domain_id="security", points=points)
    return store, embedder


async def test_retrieve_filters_by_acl_and_approval(populated_store):
    store, embedder = populated_store
    service = RetrievalService(embedder=embedder, vector_store=store)

    acl = build_qdrant_acl_filter(
        user_id="u1",
        clearance="confidential",
        department="security",
        domain_groups=["group:security"],
    )
    result = await service.retrieve(
        domain_id="security",
        question="패스워드 정책",
        acl_filter=acl,
        config=RetrievalConfig(fused_top_k=10, rerank_top_k=10, context_top_k=10),
    )
    ids = [c.chunk_id for c in result.contexts]
    # c4 (group:hr) 제외, c5 (draft) 제외
    assert "c4" not in ids
    assert "c5" not in ids
    # c3 (confidential)는 user clearance가 confidential이므로 통과
    assert "c3" in ids
    # 패스워드 관련이 top
    assert ids[0] == "c1"


async def test_retrieve_blocks_high_security_for_internal_user(populated_store):
    store, embedder = populated_store
    service = RetrievalService(embedder=embedder, vector_store=store)
    acl = build_qdrant_acl_filter(
        user_id="u2",
        clearance="internal",
        department="security",
        domain_groups=["group:security"],
    )
    result = await service.retrieve(
        domain_id="security",
        question="보안 사고 대응",
        acl_filter=acl,
    )
    ids = [c.chunk_id for c in result.contexts]
    assert "c3" not in ids  # confidential 차단


async def test_retrieve_context_top_k_truncation(populated_store):
    store, embedder = populated_store
    service = RetrievalService(embedder=embedder, vector_store=store)
    acl = build_qdrant_acl_filter(
        user_id="u1",
        clearance="confidential",
        department="security",
        domain_groups=["group:security"],
    )
    result = await service.retrieve(
        domain_id="security",
        question="패스워드",
        acl_filter=acl,
        config=RetrievalConfig(fused_top_k=10, rerank_top_k=5, context_top_k=2),
    )
    assert len(result.contexts) <= 2
    assert len(result.reranked) <= 5


async def test_retrieve_with_reranker_reorders(populated_store):
    store, embedder = populated_store
    reranker = InMemoryReranker()
    service = RetrievalService(
        embedder=embedder, vector_store=store, reranker=reranker
    )
    acl = build_qdrant_acl_filter(
        user_id="u1",
        clearance="confidential",
        department="security",
        domain_groups=["group:security"],
    )
    result = await service.retrieve(
        domain_id="security",
        question="VPN 토큰 발급",
        acl_filter=acl,
        config=RetrievalConfig(fused_top_k=10, rerank_top_k=10, context_top_k=3),
    )
    # rerank 후 첫 번째는 VPN 관련 c2여야 함 (Jaccard "VPN 토큰 발급")
    assert result.contexts[0].chunk_id == "c2"
    assert result.contexts[0].rerank_score is not None


async def test_retrieve_with_reranker_bypass(populated_store):
    store, embedder = populated_store
    reranker = InMemoryReranker()
    service = RetrievalService(
        embedder=embedder, vector_store=store, reranker=reranker
    )
    acl = build_qdrant_acl_filter(
        user_id="u1",
        clearance="confidential",
        department="security",
        domain_groups=["group:security"],
    )
    result = await service.retrieve(
        domain_id="security",
        question="패스워드",
        acl_filter=acl,
        config=RetrievalConfig(reranker_bypass=True),
    )
    # bypass 시 rerank_score는 부여되지 않음
    assert all(c.rerank_score is None for c in result.contexts)
