"""classify_query + model_router e2e — chat_logs.classifier_decision/routing_decision 확인."""

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
from rag_core.services.model_router import ModelRouter
from rag_core.services.query_classifier import QueryClassifier
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
    await store.create_collection(domain_id="security", dense_dim=64)
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
    await store.upsert_chunks(domain_id="security", points=points)
    return store, embedder


def _user() -> dict:
    return {
        "user_id": "u1", "domain_id": "security",
        "clearance": "confidential", "department": "security",
        "domain_groups": ["group:security"], "roles": ["USER"],
    }


def _config_loader_with_classifier_and_routing(_t: str) -> dict:
    """평가 임계 + 실제 platform query_classifier.yaml + routing.yaml + tenant model.yaml."""
    import yaml
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
        "query_classifier": yaml.safe_load(
            (REPO / "configs/platform/query_classifier.yaml").read_text(encoding="utf-8")
        ),
        "routing": yaml.safe_load(
            (REPO / "configs/platform/routing.yaml").read_text(encoding="utf-8")
        ),
        "model": {
            "tenant_slm": {"endpoint": "vllm-tenant", "lora_adapter": "security-policy-v1"},
            "shared_llm": {"endpoint": "vllm-shared", "lora_adapter": None},
        },
    }


def _build_deps(populated, gen_llm, *, classifier_llm) -> RAGGraphDeps:
    store, embedder = populated
    reranker = InMemoryReranker()
    retrieval = RetrievalService(
        embedder=embedder, vector_store=store, reranker=reranker
    )
    prompt = GenerationPrompt.load(
        prompt_yaml=REPO / "configs/platform/prompts/rag_answer.yaml",
        schema_json=REPO / "configs/platform/prompts/answer_schema.json",
    )
    generation = GenerationService(llm=gen_llm, prompt=prompt, model="tenant_slm")
    verifier = VerifierService(embedder=embedder)
    judge_prompt = JudgePrompt.load(
        prompt_yaml=REPO / "configs/platform/prompts/inference_judge.yaml",
        schema_json=REPO / "configs/platform/prompts/inference_judge_schema.json",
    )
    judge = JudgeService(
        llm=InMemoryLLMClient(responses=['{"valid": false, "confidence": 0.0, "reasoning": "n/a"}']),
        prompt=judge_prompt, model="qwen2.5-7b-awq",
    )
    classifier_prompt = QueryClassifier.load_tier2_prompt(
        REPO / "configs/platform/prompts/query_classifier_tier2.yaml"
    )
    classifier = QueryClassifier(
        llm=classifier_llm, prompt=classifier_prompt, model="tenant_slm"
    )
    router = ModelRouter()
    writer = InMemoryChatLogWriter()
    return RAGGraphDeps(
        retrieval_service=retrieval,
        generation_service=generation,
        config_loader=_config_loader_with_classifier_and_routing,
        today_provider=lambda: date(2026, 5, 9),
        verifier_service=verifier,
        judge_service=judge,
        chat_log_writer=writer,
        query_classifier=classifier,
        model_router=router,
    )


async def test_synthesis_pattern_routes_to_synthesis_rule(populated_corpus):
    """tier1 synthesis_pattern 매치 → routing rule synthesis_answer 매치 → tenant_slm + LoRA."""
    gen_response = json.dumps(
        {"answer_segments": [
            {"text": "패스워드 만료 주기는 90일입니다", "citations": [1], "support_type": "direct"},
        ], "limitations": None}, ensure_ascii=False,
    )
    deps = _build_deps(
        populated_corpus,
        InMemoryLLMClient(responses=[gen_response]),
        classifier_llm=InMemoryLLMClient(responses=["unused"]),  # tier1로 매치되어 호출 X
    )
    graph = build_chat_structured_full(deps)
    state = RAGState(request_id="r1", domain_id="security", user_id="u1",
                     question="A와 B의 차이를 비교해 주세요",
                     user_context=_user())
    result = await graph.ainvoke(state)

    assert result["query_type"] == "document_qa"
    assert result["support_type"] == "synthesis"
    assert result["complexity"] == "medium"
    assert result["matched_rule"] == "synthesis_answer"
    assert result["selected_model"] == "tenant_slm"
    # tenant model.yaml의 lora_adapter가 use_lora=true일 때 해석됨
    assert result["selected_lora"] == "security-policy-v1"
    # chat_logs에 두 decision 모두 채움
    rec = deps.chat_log_writer.records[0]
    assert rec.classifier_decision["tier1_matched"] == "synthesis_pattern"
    assert rec.classifier_decision["tier2_called"] is False
    assert rec.routing_decision["selected_model"] == "tenant_slm"
    assert rec.routing_decision["selected_lora"] == "security-policy-v1"


async def test_inference_pattern_routes_to_shared_llm(populated_corpus):
    """tier1 inference_pattern → routing rule inference_answer → shared_llm (LoRA off)."""
    gen_response = json.dumps(
        {"answer_segments": [
            {"text": "패스워드 만료 주기는 90일입니다", "citations": [1], "support_type": "direct"},
        ], "limitations": None}, ensure_ascii=False,
    )
    deps = _build_deps(
        populated_corpus,
        InMemoryLLMClient(responses=[gen_response]),
        classifier_llm=InMemoryLLMClient(responses=["unused"]),
    )
    graph = build_chat_structured_full(deps)
    state = RAGState(request_id="r2", domain_id="security", user_id="u1",
                     question="이 경우 반출이 허용되는가?",
                     user_context=_user())
    result = await graph.ainvoke(state)
    assert result["support_type"] == "inference"
    assert result["matched_rule"] == "inference_answer"
    assert result["selected_model"] == "shared_llm"
    assert result["selected_lora"] is None


async def test_tier2_fallback_path(populated_corpus):
    """tier1 매칭 안 됨 → tier2 LLM 호출 (mock JSON 반환) → routing default."""
    gen_response = json.dumps(
        {"answer_segments": [
            {"text": "패스워드 만료 주기는 90일입니다", "citations": [1], "support_type": "direct"},
        ], "limitations": None}, ensure_ascii=False,
    )
    tier2_response = json.dumps(
        {"query_type": "document_qa", "support_type": "direct", "complexity": "medium"}
    )
    deps = _build_deps(
        populated_corpus,
        InMemoryLLMClient(responses=[gen_response]),
        classifier_llm=InMemoryLLMClient(responses=[tier2_response]),
    )
    graph = build_chat_structured_full(deps)
    state = RAGState(request_id="r3", domain_id="security", user_id="u1",
                     question="패스워드 정책의 길이 기준이 어떻게 되나요?",
                     user_context=_user())
    result = await graph.ainvoke(state)
    rec = deps.chat_log_writer.records[0]
    assert rec.classifier_decision["tier1_matched"] is None
    assert rec.classifier_decision["tier2_called"] is True
    assert rec.classifier_decision["tier2_parse_ok"] is True
    # default routing
    assert rec.routing_decision["selected_model"] == "tenant_slm"
