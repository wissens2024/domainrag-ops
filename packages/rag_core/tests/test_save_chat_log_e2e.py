"""save_chat_log e2e — full graph가 success/fallback 양쪽 모두 chat_logs를 기록.

검증:
  - excerpt audit truth (CLAUDE.md 원칙 10)
  - 모든 verifier_metrics·routing_decision·inference_judge_results·ui_mode 보존
  - fallback 응답도 저장 (CLAUDE.md 원칙 9)
  - conversation_id 자동 발급 또는 caller 지정값 보존
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
from rag_core.services.chat_log_writer import InMemoryChatLogWriter
from rag_core.services.generation_service import (
    GenerationPrompt,
    GenerationService,
)
from rag_core.services.judge_service import JudgePrompt, JudgeService
from rag_core.services.retrieval_service import RetrievalService
from rag_core.services.verifier_service import VerifierService
from rag_core.workflows import (
    RAGGraphDeps,
    RAGState,
    build_chat_structured_full,
)


REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
async def populated_corpus():
    store = InMemoryVectorStore()
    embedder = InMemoryEmbedder(dense_dim=64)
    await store.create_collection(tenant_id="security", dense_dim=64)
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
    ]
    points = []
    for cid, text, payload in docs:
        d, s = await embedder.embed_query(text)
        points.append({"id": cid, "dense_vector": d, "sparse_vector": s, "payload": payload})
    await store.upsert_chunks(tenant_id="security", points=points)
    return store, embedder


def _user() -> dict:
    return {
        "user_id": "u1", "tenant_id": "security",
        "clearance": "confidential", "department": "security",
        "domain_groups": ["group:security"], "roles": ["USER"],
    }


def _config_loader(_t: str) -> dict:
    return {
        "retrieval": {"top_k": {"fused": 50, "rerank": 10, "context": 5}},
        "citation": {
            "verification": {"tier2": {"thresholds": {"strong": 0.99, "medium": 0.85}}},
            "gates": {"retrieval": {"min_top1_rerank": 0.0, "min_strong_chunks": 0,
                                    "strong_chunk_threshold": 0.0},
                      "generation": {"min_verified_count": 1, "max_unsupported_ratio": 0.5,
                                     "min_confidence": 0.3}},
            "confidence_weights": {"retrieval": 0.30, "verified": 0.30,
                                   "supported": 0.20, "coverage": 0.20},
        },
    }


def _build_deps(populated, gen_llm, writer) -> RAGGraphDeps:
    store, embedder = populated
    reranker = InMemoryReranker()
    retrieval = RetrievalService(
        embedder=embedder, vector_store=store, reranker=reranker
    )
    prompt = GenerationPrompt.load(
        prompt_yaml=REPO / "configs/platform/prompts/rag_answer.yaml",
        schema_json=REPO / "configs/platform/prompts/answer_schema.json",
    )
    generation = GenerationService(llm=gen_llm, prompt=prompt, model="qwen-7b")
    verifier = VerifierService(embedder=embedder)
    judge_prompt = JudgePrompt.load(
        prompt_yaml=REPO / "configs/platform/prompts/inference_judge.yaml",
        schema_json=REPO / "configs/platform/prompts/inference_judge_schema.json",
    )
    judge = JudgeService(
        llm=InMemoryLLMClient(responses=['{"valid": false, "confidence": 0.0, "reasoning": "n/a"}']),
        prompt=judge_prompt,
        model="mock",
    )
    return RAGGraphDeps(
        retrieval_service=retrieval,
        generation_service=generation,
        config_loader=_config_loader,
        today_provider=lambda: date(2026, 5, 9),
        verifier_service=verifier,
        judge_service=judge,
        chat_log_writer=writer,
    )


async def test_success_path_persists_full_chat_log(populated_corpus):
    """gate_2 통과 응답이 모든 컬럼을 갖춘 chat_logs 행으로 저장된다."""
    gen_response = json.dumps(
        {
            "answer_segments": [
                {"text": "패스워드 만료 주기는 90일입니다", "citations": [1],
                 "support_type": "direct"},
                {"text": "패스워드는 12자 이상이어야 합니다", "citations": [2],
                 "support_type": "direct"},
            ],
            "limitations": None,
        },
        ensure_ascii=False,
    )
    writer = InMemoryChatLogWriter()
    deps = _build_deps(populated_corpus, InMemoryLLMClient(responses=[gen_response]), writer)
    graph = build_chat_structured_full(deps)
    state = RAGState(request_id="r1", tenant_id="security", user_id="u1",
                     question="패스워드 정책은?", user_context=_user(),
                     selected_model="qwen-7b")
    result = await graph.ainvoke(state)

    assert result["gate2_passed"] is True
    assert len(writer.records) == 1
    rec = writer.records[0]
    assert rec.tenant_id == "security"
    assert rec.request_id == "r1"
    assert rec.user_id == "u1"
    assert rec.conversation_id  # 자동 발급
    # success 응답은 fallback_reason None
    assert rec.fallback_reason is None
    assert rec.confidence >= 0.3
    assert rec.citation_types == ["direct", "direct"]
    assert len(rec.citations) == 2
    # 모든 citation에 verifier 산출이 채워짐
    for c in rec.citations:
        assert c["support_level"] in {"strong", "medium"}
        assert c["verified"] is True
        assert c["excerpt"]  # audit truth
    # excerpt audit truth — content가 그대로 보존
    assert len(rec.retrieved_chunks) >= 1
    assert all("content" in rc for rc in rec.retrieved_chunks)
    # verifier_metrics
    assert "tier1_markers_removed" in rec.verifier_metrics
    assert "tier2_avg_similarity" in rec.verifier_metrics
    # routing_decision — deps에 classifier/router 미주입 시 default 라우팅 (model="tenant_slm")
    assert rec.routing_decision["selected_model"] == "tenant_slm"
    assert rec.routing_decision["ui_mode"] == "chat_structured"
    # ui_mode 컬럼
    assert rec.ui_mode == "chat_structured"


async def test_fallback_path_also_persists(populated_corpus):
    """gate_2 fail 응답도 chat_logs에 저장된다 (CLAUDE.md 원칙 9)."""
    # 모든 segment unsupported로 만들어 gate_2 fail 유도
    gen_response = json.dumps(
        {
            "answer_segments": [
                {"text": "확인 불가 1", "citations": [], "support_type": "direct"},
                {"text": "확인 불가 2", "citations": [], "support_type": "direct"},
            ],
            "limitations": None,
        },
        ensure_ascii=False,
    )
    writer = InMemoryChatLogWriter()
    deps = _build_deps(populated_corpus, InMemoryLLMClient(responses=[gen_response]), writer)
    graph = build_chat_structured_full(deps)
    state = RAGState(request_id="r2", tenant_id="security", user_id="u1",
                     question="패스워드 정책", user_context=_user())
    result = await graph.ainvoke(state)

    assert result["gate2_passed"] is False
    assert len(writer.records) == 1
    rec = writer.records[0]
    assert rec.fallback_reason is not None
    assert rec.fallback_reason.startswith("gate2_")
    assert rec.confidence == 0.0
    assert rec.citations == []
    # answer는 fallback_node가 채운 안내 문구
    assert "근거" in rec.answer or "확인" in rec.answer


async def test_caller_supplied_conversation_id_preserved(populated_corpus):
    gen_response = json.dumps(
        {
            "answer_segments": [
                {"text": "패스워드 만료 주기는 90일입니다", "citations": [1],
                 "support_type": "direct"},
            ],
            "limitations": None,
        },
        ensure_ascii=False,
    )
    writer = InMemoryChatLogWriter()
    deps = _build_deps(populated_corpus, InMemoryLLMClient(responses=[gen_response]), writer)
    graph = build_chat_structured_full(deps)
    state = RAGState(request_id="r3", tenant_id="security", user_id="u1",
                     conversation_id="conv-existing-123",
                     question="패스워드 정책은?", user_context=_user())
    result = await graph.ainvoke(state)
    assert result["conversation_id"] == "conv-existing-123"
    assert writer.records[0].conversation_id == "conv-existing-123"


async def test_no_writer_does_not_break_graph(populated_corpus):
    """deps.chat_log_writer=None이면 노드는 no-op (DB-less 환경 지원)."""
    gen_response = json.dumps(
        {
            "answer_segments": [
                {"text": "패스워드 만료 주기는 90일입니다", "citations": [1],
                 "support_type": "direct"},
            ],
            "limitations": None,
        },
        ensure_ascii=False,
    )
    deps = _build_deps(populated_corpus, InMemoryLLMClient(responses=[gen_response]), None)
    graph = build_chat_structured_full(deps)
    state = RAGState(request_id="r4", tenant_id="security", user_id="u1",
                     question="패스워드 정책은?", user_context=_user())
    result = await graph.ainvoke(state)
    # 그래프는 정상 종료. conversation_id는 입력 None 그대로.
    assert result["gate2_passed"] is True
