"""chat_structured vertical slice e2e — 6 노드 + fallback (InMemory mock).

build_chat_structured_slice(deps) → ainvoke(state) → 결과 검증.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from rag_core.clients.in_memory import (
    InMemoryEmbedder,
    InMemoryLLMClient,
    InMemoryReranker,
    InMemoryVectorStore,
)
from rag_core.services.generation_service import (
    GenerationPrompt,
    GenerationService,
)
from rag_core.services.retrieval_service import RetrievalService
from rag_core.workflows import (
    RAGGraphDeps,
    RAGState,
    build_chat_structured_slice,
)


REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
async def populated_corpus():
    store = InMemoryVectorStore()
    embedder = InMemoryEmbedder(dense_dim=8)
    await store.create_collection(tenant_id="security", dense_dim=8)

    # 보안 도메인 4문서 (모두 approved + group:security)
    docs = [
        ("c1", "패스워드는 12자 이상이어야 합니다",
         {"approval_status": "approved", "security_level": "internal",
          "acl": ["group:security"], "doc_id": "d1",
          "title": "패스워드 정책", "page_number": 1, "section_title": "기본",
          "content": "패스워드는 12자 이상이어야 합니다"}),
        ("c2", "패스워드 만료 주기는 90일입니다",
         {"approval_status": "approved", "security_level": "internal",
          "acl": ["group:security"], "doc_id": "d1",
          "title": "패스워드 정책", "page_number": 2, "section_title": "만료",
          "content": "패스워드 만료 주기는 90일입니다"}),
        ("c3", "VPN 접속은 사내 토큰 발급 후 가능합니다",
         {"approval_status": "approved", "security_level": "internal",
          "acl": ["group:security"], "doc_id": "d2",
          "title": "VPN 정책", "page_number": 1, "section_title": "접속",
          "content": "VPN 접속은 사내 토큰 발급 후 가능합니다"}),
        ("c4", "사고 대응 절차 — 보안팀 즉시 알림",
         {"approval_status": "approved", "security_level": "confidential",
          "acl": ["group:security"], "doc_id": "d3",
          "title": "사고 대응", "page_number": 1, "section_title": "프로세스",
          "content": "사고 대응 절차 — 보안팀 즉시 알림"}),
    ]
    points = []
    for cid, text, payload in docs:
        dense, sparse = await embedder.embed_query(text)
        points.append(
            {"id": cid, "dense_vector": dense, "sparse_vector": sparse, "payload": payload}
        )
    await store.upsert_chunks(tenant_id="security", points=points)
    return store, embedder


def _user_context() -> dict:
    return {
        "user_id": "u1",
        "tenant_id": "security",
        "clearance": "confidential",
        "department": "security",
        "domain_groups": ["group:security"],
        "roles": ["USER"],
    }


def _config_loader_factory(min_top1: float = 0.0, min_strong: int = 0):
    def loader(tenant_id: str) -> dict:
        return {
            "retrieval": {"top_k": {"fused": 50, "rerank": 10, "context": 5}},
            "citation": {
                "gates": {
                    "retrieval": {
                        "min_top1_rerank": min_top1,
                        "min_strong_chunks": min_strong,
                        "strong_chunk_threshold": 0.0,
                    }
                }
            },
        }

    return loader


def _build_deps(populated, llm: InMemoryLLMClient, *, min_top1=0.0, min_strong=0):
    store, embedder = populated
    reranker = InMemoryReranker()
    retrieval = RetrievalService(
        embedder=embedder, vector_store=store, reranker=reranker
    )
    prompt = GenerationPrompt.load(
        prompt_yaml=REPO / "configs/platform/prompts/rag_answer.yaml",
        schema_json=REPO / "configs/platform/prompts/answer_schema.json",
    )
    generation = GenerationService(llm=llm, prompt=prompt, model="qwen-7b")
    return RAGGraphDeps(
        retrieval_service=retrieval,
        generation_service=generation,
        config_loader=_config_loader_factory(min_top1=min_top1, min_strong=min_strong),
        today_provider=lambda: date(2026, 5, 9),
    )


async def test_happy_path_returns_structured_answer(populated_corpus):
    llm_response = json.dumps(
        {
            "answer_segments": [
                {"text": "패스워드는 12자 이상", "citations": [1], "support_type": "direct"},
                {"text": "만료 주기는 90일", "citations": [2], "support_type": "direct"},
            ],
            "limitations": None,
        },
        ensure_ascii=False,
    )
    llm = InMemoryLLMClient(responses=[llm_response])
    deps = _build_deps(populated_corpus, llm)

    graph = build_chat_structured_slice(deps)
    state = RAGState(
        request_id="r1",
        tenant_id="security",
        user_id="u1",
        question="패스워드 정책은?",
        user_context=_user_context(),
    )
    result = await graph.ainvoke(state)

    assert result["fallback_reason"] is None or result.get("gate1_passed") is True
    assert result["gate1_passed"] is True
    assert len(result["answer_segments"]) == 2
    assert result["answer_segments"][0]["citations"] == [1]
    assert result["limitations"] is None
    assert len(result["final_contexts"]) > 0
    # ACL 필터에서 group:security가 통과 — 4문서 전부 후보
    assert len(result["retrieved_chunks"]) == 4
    # LLM이 정확히 1번 호출됨
    assert len([c for c in llm.calls if c["kind"] == "generate"]) == 1


async def test_gate1_fail_routes_to_fallback(populated_corpus):
    llm = InMemoryLLMClient(responses=['should not be called'])
    # min_strong=99로 두면 절대 통과 못함
    deps = _build_deps(populated_corpus, llm, min_top1=0.99, min_strong=99)

    graph = build_chat_structured_slice(deps)
    state = RAGState(
        request_id="r2",
        tenant_id="security",
        user_id="u1",
        question="패스워드 정책",
        user_context=_user_context(),
    )
    result = await graph.ainvoke(state)

    assert result["gate1_passed"] is False
    assert result["fallback_reason"] == "gate1_failed"
    # fallback 응답 메시지
    assert len(result["answer_segments"]) == 1
    assert result["answer_segments"][0]["support_type"] == "fallback"
    assert result["confidence"] == 0.0
    # 생성 노드는 호출되지 않았음
    assert all(c["kind"] != "generate" for c in llm.calls) or not llm.calls


async def test_acl_blocks_unauthorized_user(populated_corpus):
    """다른 그룹 사용자는 group:security 문서에 접근 불가 → fallback."""
    llm = InMemoryLLMClient(responses=['unused'])
    deps = _build_deps(populated_corpus, llm, min_top1=0.0, min_strong=1)

    graph = build_chat_structured_slice(deps)
    foreign_user = {
        "user_id": "u9",
        "tenant_id": "security",
        "clearance": "internal",
        "department": "hr",
        "domain_groups": ["group:hr"],  # security 문서 접근 불가
        "roles": ["USER"],
    }
    state = RAGState(
        request_id="r3",
        tenant_id="security",
        user_id="u9",
        question="패스워드 정책",
        user_context=foreign_user,
    )
    result = await graph.ainvoke(state)

    assert result["retrieved_chunks"] == []
    assert result["gate1_passed"] is False
    assert result["fallback_reason"] in {"retrieval_unavailable", "gate1_failed"}


async def test_invalid_llm_response_routes_to_parse_error(populated_corpus):
    llm = InMemoryLLMClient(responses=["totally not json"])
    deps = _build_deps(populated_corpus, llm)
    graph = build_chat_structured_slice(deps)
    state = RAGState(
        request_id="r4",
        tenant_id="security",
        user_id="u1",
        question="패스워드?",
        user_context=_user_context(),
    )
    result = await graph.ainvoke(state)
    assert result["fallback_reason"] == "generation_parse_error"
    assert result["raw_response"] == "totally not json"
    assert result["error"] is not None
