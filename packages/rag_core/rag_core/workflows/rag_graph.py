"""
RAG LangGraph workflow — ADR-013 §9 통합 흐름.

흐름 (ADR-008/010/011/013/018/020 정합):
  tenant_resolver → load_tenant_config → check_input_pii → build_acl_filter
  → classify_query → model_router → query_rewrite (조건부)
  → retrieve_context → [Gate 1]
  → generate_answer (sync/streaming 분기)
  → parse_response → verify_tier1 → detect_conflict_heuristic
  → verify_tier2 → judge_inference (조건부) → detect_unsupported
  → assemble_response → mask_response_pii
  → [Gate 2] → save_chat_log → END

각 노드는 service class를 호출하는 얇은 래퍼 (ADR-003 원칙).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RAGState:
    """LangGraph state (ADR-013 §9 + ADR-018·020 보강)."""

    request_id: str
    domain_id: str
    user_id: str
    conversation_id: str | None = None
    question: str = ""

    # ADR-018 §5
    user_context: dict | None = None

    # ADR-009
    tenant_config: dict | None = None

    # ADR-004 + ADR-011 §9
    acl_filter: dict | None = None

    # ADR-013 classifier
    query_type: str | None = None
    support_type: str | None = None
    complexity: str | None = None
    classifier_decision: dict = field(default_factory=dict)

    # routing
    matched_rule: str | None = None
    selected_model: str | None = None
    selected_lora: str | None = None
    use_rag: bool = True
    ui_mode: str = "chat_structured"
    # ADR-023 §4 — 근거 유무 분기 결과. grounded=4-type citation 검증 경로,
    # ungrounded=일반 대화(인용 없음, UI 배지로 명시). status=success일 때만 의미.
    grounding: str = "grounded"

    # query rewrite
    rewritten_query: str = ""

    # retrieval
    retrieved_chunks: list[dict] = field(default_factory=list)
    reranked_chunks: list[dict] = field(default_factory=list)
    final_contexts: list[dict] = field(default_factory=list)

    # gate 1 (ADR-010)
    gate1_passed: bool = False
    gate1_metrics: dict = field(default_factory=dict)

    # ADR-020 PII
    input_pii_found: list[dict] = field(default_factory=list)
    input_blocked: bool = False
    blocked_categories: list[str] = field(default_factory=list)
    # Layer 2 (ADR-020 §4) — chat_logs 보관용 question. mask 정책 시 마스킹된 form,
    # plain 정책 시 빈 문자열(save_chat_log_node가 state.question을 그대로 사용).
    question_for_storage: str = ""
    pii_storage_policy: str = "mask"

    # generation
    raw_response: str = ""
    answer_segments: list[dict] = field(default_factory=list)
    limitations: str | None = None

    # verifier (ADR-010)
    citations: list[dict] = field(default_factory=list)
    citation_types: list[str] = field(default_factory=list)
    verifier_metrics: dict = field(default_factory=dict)
    inference_judge_results: list[dict] = field(default_factory=list)
    # 각 entry: {segment_index, signal, groups: list[list[int]], details, source: 'heuristic'|'llm_primary'}
    conflict_groups: list[dict] = field(default_factory=list)
    unsupported_ratio: float = 0.0

    # final
    final_answer: str = ""
    output_pii_masked: list[dict] = field(default_factory=list)

    # gate 2
    gate2_passed: bool = False
    confidence: float = 0.0

    # fallback
    fallback_reason: str | None = None
    near_misses: list[dict] = field(default_factory=list)

    # logging
    model_failure_chain: list[dict] = field(default_factory=list)
    latency_ms: int = 0
    error: str | None = None


def build_rag_graph() -> Any:
    """LangGraph StateGraph 빌더 — 전체(21노드) 골격.

    각 노드는 stub. 실제 운영 결선은 카테고리별로 build_chat_structured_slice 등으로
    분리되어 진행 중. 본 함수는 ADR-013 §9 그래프 토폴로지를 코드로 추적하는 reference.
    """
    from langgraph.graph import END, StateGraph

    graph = StateGraph(RAGState)

    def stub_node(state: RAGState) -> RAGState:
        return state

    nodes = [
        "tenant_resolver",
        "load_tenant_config",
        "check_input_pii",
        "build_acl_filter",
        "classify_query",
        "model_router",
        "query_rewrite",
        "retrieve_context",
        "gate_1",
        "generate_answer",
        "parse_response",
        "verify_tier1",
        "detect_conflict_heuristic",
        "verify_tier2",
        "detect_unsupported",
        "judge_inference",
        "assemble_response",
        "mask_response_pii",
        "compute_confidence",
        "gate_2",
        "save_chat_log",
        "fallback",
    ]
    for n in nodes:
        graph.add_node(n, stub_node)

    graph.set_entry_point("tenant_resolver")
    graph.add_edge("tenant_resolver", "load_tenant_config")
    graph.add_edge("load_tenant_config", "build_acl_filter")
    graph.add_edge("build_acl_filter", "classify_query")
    graph.add_edge("classify_query", "model_router")
    graph.add_edge("model_router", "query_rewrite")
    graph.add_edge("query_rewrite", "retrieve_context")
    graph.add_edge("retrieve_context", "gate_1")
    graph.add_edge("check_input_pii", "build_acl_filter")
    graph.add_edge("generate_answer", "parse_response")
    graph.add_edge("parse_response", "verify_tier1")
    graph.add_edge("verify_tier1", "detect_conflict_heuristic")
    graph.add_edge("detect_conflict_heuristic", "verify_tier2")
    graph.add_edge("verify_tier2", "detect_unsupported")
    graph.add_edge("detect_unsupported", "judge_inference")
    graph.add_edge("judge_inference", "assemble_response")
    graph.add_edge("assemble_response", "mask_response_pii")
    graph.add_edge("mask_response_pii", "compute_confidence")
    graph.add_edge("compute_confidence", "gate_2")
    graph.add_edge("save_chat_log", END)
    graph.add_edge("fallback", "save_chat_log")

    return graph.compile()


def build_chat_structured_full(deps) -> Any:
    """chat_structured 완전 결선 — 21노드 그래프 (ADR-013 §9 + ADR-020 PII + ADR-010 §7 conflict + ADR-011 §5 query rewrite).

    노드 (21):
      tenant_resolver, load_tenant_config, check_input_pii, build_acl_filter,
      classify_query, model_router, query_rewrite, retrieve_context, gate_1,
      generate_answer, parse_response, verify_tier1, detect_conflict_heuristic,
      verify_tier2, judge_inference, detect_unsupported, assemble_response,
      mask_response_pii, compute_confidence, gate_2, save_chat_log, fallback

    분기:
      check_input_pii blocked → fallback → save_chat_log → END
      gate_1 fail             → fallback → save_chat_log → END
      gate_2 fail             → fallback → save_chat_log → END
      gate_2 pass             → save_chat_log → END

    inference·conflict 타입은 VerifierService 내부에서 fail-safe로 다운그레이드된다.

    Args:
        deps: rag_core.workflows.nodes.RAGGraphDeps (verifier_service 필수;
              pii_service None이면 PII 노드는 no-op)
    """
    from langgraph.graph import END, StateGraph

    from .nodes import (
        assemble_response_node,
        build_acl_filter_node,
        check_input_pii_node,
        check_input_pii_router,
        classify_query_node,
        compute_confidence_node,
        detect_conflict_heuristic_node,
        detect_unsupported_node,
        fallback_node,
        gate_1_full_router,
        gate_1_node,
        gate_2_node,
        gate_2_router,
        generate_answer_node,
        generate_ungrounded_node,
        judge_inference_node,
        load_tenant_config_node,
        mask_response_pii_node,
        model_router_node,
        parse_response_node,
        post_mask_grounding_router,
        query_rewrite_node,
        retrieve_context_node,
        save_chat_log_node,
        tenant_resolver_node,
        verify_tier1_node,
        verify_tier2_node,
    )

    if deps.verifier_service is None:
        raise ValueError(
            "build_chat_structured_full requires deps.verifier_service "
            "(use build_chat_structured_slice for the smaller variant)"
        )

    graph = StateGraph(RAGState)

    async def _load_config(s):
        return await load_tenant_config_node(s, deps)

    async def _build_acl(s):
        return await build_acl_filter_node(s, deps)

    async def _retrieve(s):
        return await retrieve_context_node(s, deps)

    async def _generate(s):
        return await generate_answer_node(s, deps)

    async def _generate_ungrounded(s):
        return await generate_ungrounded_node(s, deps)

    async def _verify_tier2(s):
        return await verify_tier2_node(s, deps)

    async def _judge_inference(s):
        return await judge_inference_node(s, deps)

    async def _save_chat_log(s):
        return await save_chat_log_node(s, deps)

    async def _classify_query(s):
        return await classify_query_node(s, deps)

    async def _model_router(s):
        return await model_router_node(s, deps)

    async def _check_input_pii(s):
        return await check_input_pii_node(s, deps)

    async def _mask_response_pii(s):
        return await mask_response_pii_node(s, deps)

    async def _detect_conflict(s):
        return await detect_conflict_heuristic_node(s, deps)

    async def _query_rewrite(s):
        return await query_rewrite_node(s, deps)

    graph.add_node("tenant_resolver", tenant_resolver_node)
    graph.add_node("load_tenant_config", _load_config)
    graph.add_node("check_input_pii", _check_input_pii)
    graph.add_node("build_acl_filter", _build_acl)
    graph.add_node("classify_query", _classify_query)
    graph.add_node("model_router", _model_router)
    graph.add_node("query_rewrite", _query_rewrite)
    graph.add_node("retrieve_context", _retrieve)
    graph.add_node("gate_1", gate_1_node)
    graph.add_node("generate_answer", _generate)
    graph.add_node("generate_ungrounded", _generate_ungrounded)
    graph.add_node("parse_response", parse_response_node)
    graph.add_node("verify_tier1", verify_tier1_node)
    graph.add_node("detect_conflict_heuristic", _detect_conflict)
    graph.add_node("verify_tier2", _verify_tier2)
    graph.add_node("judge_inference", _judge_inference)
    graph.add_node("detect_unsupported", detect_unsupported_node)
    graph.add_node("assemble_response", assemble_response_node)
    graph.add_node("mask_response_pii", _mask_response_pii)
    graph.add_node("compute_confidence", compute_confidence_node)
    graph.add_node("gate_2", gate_2_node)
    graph.add_node("fallback", fallback_node)
    graph.add_node("save_chat_log", _save_chat_log)

    graph.set_entry_point("tenant_resolver")
    graph.add_edge("tenant_resolver", "load_tenant_config")
    graph.add_edge("load_tenant_config", "check_input_pii")
    graph.add_conditional_edges(
        "check_input_pii",
        check_input_pii_router,
        {"build_acl_filter": "build_acl_filter", "fallback": "fallback"},
    )
    graph.add_edge("build_acl_filter", "classify_query")
    graph.add_edge("classify_query", "model_router")
    # ADR-023: ui_mode 기반 streaming redirect 폐기 — 항상 검색으로 진행.
    graph.add_edge("model_router", "query_rewrite")
    graph.add_edge("query_rewrite", "retrieve_context")
    graph.add_edge("retrieve_context", "gate_1")
    # ADR-023 §3: Gate 1 = 분기점. pass → grounded(citation), fail → ungrounded(대화).
    graph.add_conditional_edges(
        "gate_1",
        gate_1_full_router,
        {
            "generate_answer": "generate_answer",
            "generate_ungrounded": "generate_ungrounded",
        },
    )
    graph.add_edge("generate_ungrounded", "mask_response_pii")
    graph.add_edge("generate_answer", "parse_response")
    graph.add_edge("parse_response", "verify_tier1")
    graph.add_edge("verify_tier1", "detect_conflict_heuristic")
    graph.add_edge("detect_conflict_heuristic", "verify_tier2")
    graph.add_edge("verify_tier2", "judge_inference")
    graph.add_edge("judge_inference", "detect_unsupported")
    graph.add_edge("detect_unsupported", "assemble_response")
    graph.add_edge("assemble_response", "mask_response_pii")
    # ADR-023 §3: ungrounded는 verify/Gate 2를 건너뛰고 바로 저장(인용 없음).
    graph.add_conditional_edges(
        "mask_response_pii",
        post_mask_grounding_router,
        {
            "compute_confidence": "compute_confidence",
            "save_chat_log": "save_chat_log",
        },
    )
    graph.add_edge("compute_confidence", "gate_2")
    graph.add_conditional_edges(
        "gate_2",
        gate_2_router,
        {"success": "save_chat_log", "fallback": "fallback"},
    )
    graph.add_edge("fallback", "save_chat_log")
    graph.add_edge("save_chat_log", END)

    return graph.compile()


def build_chat_structured_slice(deps) -> Any:
    """chat_structured 우주왕복 1회 — 6개 노드 + fallback (ADR-013 §9 부분집합).

    입력 state 요건:
      - domain_id, user_id, question, user_context (dict)
    출력 state:
      - answer_segments, limitations, raw_response, gate1_metrics, final_contexts
        (Gate 1 fail이면 fallback path: answer_segments=fallback, fallback_reason 채움)

    Args:
        deps: rag_core.workflows.nodes.RAGGraphDeps
    """
    from langgraph.graph import END, StateGraph

    from .nodes import (
        build_acl_filter_node,
        fallback_node,
        gate_1_node,
        gate_1_router,
        generate_answer_node,
        load_tenant_config_node,
        retrieve_context_node,
        tenant_resolver_node,
    )

    graph = StateGraph(RAGState)

    # deps 바인딩 — async 래퍼로 deps 주입 (sync lambda는 coroutine 미await로 실패)
    async def _load_config(s):
        return await load_tenant_config_node(s, deps)

    async def _build_acl(s):
        return await build_acl_filter_node(s, deps)

    async def _retrieve(s):
        return await retrieve_context_node(s, deps)

    async def _generate(s):
        return await generate_answer_node(s, deps)

    graph.add_node("tenant_resolver", tenant_resolver_node)
    graph.add_node("load_tenant_config", _load_config)
    graph.add_node("build_acl_filter", _build_acl)
    graph.add_node("retrieve_context", _retrieve)
    graph.add_node("gate_1", gate_1_node)
    graph.add_node("generate_answer", _generate)
    graph.add_node("fallback", fallback_node)

    graph.set_entry_point("tenant_resolver")
    graph.add_edge("tenant_resolver", "load_tenant_config")
    graph.add_edge("load_tenant_config", "build_acl_filter")
    graph.add_edge("build_acl_filter", "retrieve_context")
    graph.add_edge("retrieve_context", "gate_1")
    graph.add_conditional_edges(
        "gate_1",
        gate_1_router,
        {"generate_answer": "generate_answer", "fallback": "fallback"},
    )
    graph.add_edge("generate_answer", END)
    graph.add_edge("fallback", END)

    return graph.compile()
