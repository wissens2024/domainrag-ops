"""judge_inference 통합 e2e — Tier2 통과 후 LLM-as-judge 단계.

흐름:
  generate → parse → tier1 → tier2(inference: pending_judge 마킹)
    → judge_inference → tier3 → assemble → confidence → gate2

judge_enabled=True여야 inference가 Tier2까지 진입함.
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


def _config_loader_judge_enabled() -> dict:
    return {
        "retrieval": {"top_k": {"fused": 50, "rerank": 10, "context": 5}},
        "citation": {
            "verification": {
                "tier2": {"thresholds": {"strong": 0.99, "medium": 0.85}},
                "inference_judge": {"enable": True, "confidence_threshold": 0.6},
            },
            "gates": {
                "retrieval": {"min_top1_rerank": 0.0, "min_strong_chunks": 0,
                              "strong_chunk_threshold": 0.0},
                "generation": {"min_verified_count": 1, "max_unsupported_ratio": 0.5,
                               "min_confidence": 0.3},
            },
            "confidence_weights": {
                "retrieval": 0.30, "verified": 0.30,
                "supported": 0.20, "coverage": 0.20,
            },
        },
    }


def _build_deps(populated, gen_llm, judge_llm) -> RAGGraphDeps:
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
    judge = JudgeService(llm=judge_llm, prompt=judge_prompt, model="qwen-14b")
    return RAGGraphDeps(
        retrieval_service=retrieval,
        generation_service=generation,
        config_loader=lambda _t: _config_loader_judge_enabled(),
        today_provider=lambda: date(2026, 5, 9),
        verifier_service=verifier,
        judge_service=judge,
    )


async def test_inference_passes_judge_capped_at_medium(populated_corpus):
    """LLM이 inference segment 출력 → tier2 medium+ 통과 → judge valid+confidence>=0.6
    → support_level 'medium'으로 cap된 verified citation 노출."""
    # final_contexts 순서 c2, c1 (질문 '패스워드 정책은?' 기준)
    # inference claim text는 c2(만료 90일)와 정확히 일치하도록 잡아 Tier2 strong 통과
    # → judge로 진입 → judge 통과 → support_level cap=medium
    gen_response = json.dumps(
        {
            "answer_segments": [
                # direct (c2)
                {"text": "패스워드 만료 주기는 90일입니다", "citations": [1],
                 "support_type": "direct"},
                # inference (c2 인용, claim과 동일 문구로 Tier2 통과)
                {"text": "패스워드 만료 주기는 90일입니다",
                 "citations": [1],
                 "support_type": "inference",
                 "inference_chain": "주기 정책 → 만료 강제"},
            ],
            "limitations": None,
        },
        ensure_ascii=False,
    )
    judge_response = json.dumps(
        {"valid": True, "confidence": 0.85, "reasoning": "근거 합리적", "caveat": None}
    )
    deps = _build_deps(
        populated_corpus,
        InMemoryLLMClient(responses=[gen_response]),
        InMemoryLLMClient(responses=[judge_response]),
    )
    graph = build_chat_structured_full(deps)
    state = RAGState(request_id="r1", tenant_id="security", user_id="u1",
                     question="패스워드 정책은?", user_context=_user())
    result = await graph.ainvoke(state)

    assert result["gate2_passed"] is True
    # 두 citation 모두 노출
    types = result["citation_types"]
    assert "direct" in types
    assert "inference" in types
    # inference citation은 medium으로 cap (strong 절대 안 됨)
    inf_cites = [c for c in result["citations"] if c["support_type"] == "inference"]
    assert len(inf_cites) == 1
    assert inf_cites[0]["support_level"] == "medium"
    assert inf_cites[0]["verified"] is True
    # judge 결과가 chat_logs용 state에 기록됨
    assert len(result["inference_judge_results"]) == 1
    jr = result["inference_judge_results"][0]
    assert jr["valid"] is True
    assert jr["confidence"] == 0.85


async def test_inference_rejected_by_judge(populated_corpus):
    """judge가 invalid 판정 → inference citations 제거 + downgrade_reason='judge_rejected'."""
    gen_response = json.dumps(
        {
            "answer_segments": [
                # direct가 있어야 fallback이 아닌 success path로 도달
                {"text": "패스워드 만료 주기는 90일입니다", "citations": [1],
                 "support_type": "direct"},
                {"text": "패스워드 만료 주기는 90일입니다",
                 "citations": [1],
                 "support_type": "inference"},
            ],
            "limitations": None,
        },
        ensure_ascii=False,
    )
    judge_response = json.dumps(
        {"valid": False, "confidence": 0.2, "reasoning": "근거 부재"}
    )
    deps = _build_deps(
        populated_corpus,
        InMemoryLLMClient(responses=[gen_response]),
        InMemoryLLMClient(responses=[judge_response]),
    )
    graph = build_chat_structured_full(deps)
    state = RAGState(request_id="r2", tenant_id="security", user_id="u1",
                     question="패스워드 정책은?", user_context=_user())
    result = await graph.ainvoke(state)

    # direct는 살아남고 inference는 제거됨
    types = result["citation_types"]
    assert "direct" in types
    assert "inference" not in types
    # inference_judge_results에 거절 기록
    assert any(jr["valid"] is False for jr in result["inference_judge_results"])
    # downgrade caveat이 limitations에 추가됨
    assert result["limitations"] is not None
    assert "추론" in result["limitations"]


async def test_inference_low_confidence_below_threshold_rejected(populated_corpus):
    """valid=true이지만 confidence가 min_confidence 미만이면 거절."""
    gen_response = json.dumps(
        {
            "answer_segments": [
                {"text": "패스워드 만료 주기는 90일입니다", "citations": [1],
                 "support_type": "direct"},
                {"text": "패스워드 만료 주기는 90일입니다",
                 "citations": [1],
                 "support_type": "inference"},
            ],
            "limitations": None,
        },
        ensure_ascii=False,
    )
    judge_response = json.dumps(
        {"valid": True, "confidence": 0.55, "reasoning": "부분적 지지"}
    )
    deps = _build_deps(
        populated_corpus,
        InMemoryLLMClient(responses=[gen_response]),
        InMemoryLLMClient(responses=[judge_response]),
    )
    graph = build_chat_structured_full(deps)
    state = RAGState(request_id="r3", tenant_id="security", user_id="u1",
                     question="패스워드 정책은?", user_context=_user())
    result = await graph.ainvoke(state)

    types = result["citation_types"]
    assert "inference" not in types
    assert result["inference_judge_results"][0]["confidence"] == 0.55


async def test_inference_tier2_weak_chunk_skipped_before_judge(populated_corpus):
    """inference의 cited chunk가 Tier 2에서 weak이면 judge 호출 없이 다운그레이드."""
    gen_response = json.dumps(
        {
            "answer_segments": [
                {"text": "패스워드 만료 주기는 90일입니다", "citations": [1],
                 "support_type": "direct"},
                # 무관한 claim text로 Tier 2 weak 만들어 judge 진입 차단
                {"text": "전혀 무관한 텍스트 임의 내용",
                 "citations": [1],
                 "support_type": "inference"},
            ],
            "limitations": None,
        },
        ensure_ascii=False,
    )
    # judge_llm은 호출되지 않아야 함 — 응답 비워둠
    judge_llm = InMemoryLLMClient(responses=["should not be called"])
    deps = _build_deps(
        populated_corpus,
        InMemoryLLMClient(responses=[gen_response]),
        judge_llm,
    )
    graph = build_chat_structured_full(deps)
    state = RAGState(request_id="r4", tenant_id="security", user_id="u1",
                     question="패스워드 정책은?", user_context=_user())
    result = await graph.ainvoke(state)

    # judge_inference_results 빈 배열 (judge 호출 0회)
    assert result["inference_judge_results"] == []
    # judge LLM이 호출되지 않음
    assert all(c["kind"] != "generate" for c in judge_llm.calls)


async def test_judge_disabled_keeps_old_downgrade_path(populated_corpus):
    """citation.yaml.inference_judge.enable=False면 inference는 항상 다운그레이드.

    backward compatibility — 이전 세션과 동일한 동작을 보존."""
    gen_response = json.dumps(
        {
            "answer_segments": [
                {"text": "패스워드 만료 주기는 90일입니다", "citations": [1],
                 "support_type": "direct"},
                {"text": "패스워드 만료 주기는 90일입니다",
                 "citations": [1], "support_type": "inference"},
            ],
            "limitations": None,
        },
        ensure_ascii=False,
    )
    judge_llm = InMemoryLLMClient(responses=["unused"])
    deps = _build_deps(
        populated_corpus,
        InMemoryLLMClient(responses=[gen_response]),
        judge_llm,
    )
    # judge 비활성으로 override
    def _cfg(_t):
        cfg = _config_loader_judge_enabled()
        cfg["citation"]["verification"]["inference_judge"]["enable"] = False
        return cfg
    deps.config_loader = _cfg

    graph = build_chat_structured_full(deps)
    state = RAGState(request_id="r5", tenant_id="security", user_id="u1",
                     question="패스워드 정책은?", user_context=_user())
    result = await graph.ainvoke(state)
    assert "inference" not in result["citation_types"]
    assert result["inference_judge_results"] == []
    # judge LLM 미호출
    assert all(c["kind"] != "generate" for c in judge_llm.calls)
