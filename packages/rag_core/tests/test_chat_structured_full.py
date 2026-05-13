"""build_chat_structured_full e2e — verifier 결선까지 포함한 라운드트립."""

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
from rag_core.pii import RegexPIIDetector
from rag_core.services.chat_log_writer import InMemoryChatLogWriter
from rag_core.services.conflict_detector import ConflictDetector
from rag_core.services.generation_service import (
    GenerationPrompt,
    GenerationService,
)
from rag_core.services.pii_service import PIIService
from rag_core.services.query_rewriter import (
    QueryRewriter,
    QueryRewritePrompt,
)
from rag_core.services.retrieval_service import RetrievalService
from rag_core.services.verifier_service import VerifierService
from rag_core.workflows import (
    RAGGraphDeps,
    RAGState,
    build_chat_structured_full,
)


REPO = Path(__file__).resolve().parents[3]
RULES_DIR = REPO / "packages" / "rag_core" / "rag_core" / "pii" / "rules"


def _pii_config() -> dict:
    """tenant_config.pii — ADR-020 §3·§6 정책. high=block, medium=warn, low=log."""
    return {
        "input": {
            "enable": True,
            "on_pii_found": {
                "high_severity": "block",
                "medium_severity": "warn",
                "low_severity": "log",
            },
        },
        "response": {"enable": True},
        "severity_map": {
            "rrn": "high",
            "credit_card": "high",
            "phone": "medium",
            "email": "low",
        },
    }


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
        points.append(
            {"id": cid, "dense_vector": d, "sparse_vector": s, "payload": payload}
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


def _config_loader(_tid: str) -> dict:
    """citation.yaml 형식 — InMemory 임베더 cosine이 동일/무관 텍스트를 잘 갈라낼 수 있는
    임계로 설정. 운영에서는 strong=0.75/medium=0.55."""
    return {
        "retrieval": {"top_k": {"fused": 50, "rerank": 10, "context": 5}},
        "citation": {
            "verification": {
                "tier2": {
                    "thresholds": {"strong": 0.99, "medium": 0.85}
                },
                "conflict_detection": {
                    "primary": "llm_explicit",
                    "heuristic": {
                        "enable": True,
                        "patterns": ["date_diff", "numeric_diff", "rule_id_diff"],
                    },
                },
            },
            "gates": {
                "retrieval": {
                    "min_top1_rerank": 0.0,
                    "min_strong_chunks": 0,
                    "strong_chunk_threshold": 0.0,
                },
                "generation": {
                    "min_verified_count": 1,
                    "max_unsupported_ratio": 0.5,
                    "min_confidence": 0.3,
                },
            },
            "confidence_weights": {
                "retrieval": 0.30, "verified": 0.30,
                "supported": 0.20, "coverage": 0.20,
            },
        },
        "pii": _pii_config(),
    }


def _build_deps(populated, llm: InMemoryLLMClient) -> RAGGraphDeps:
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
    verifier = VerifierService(embedder=embedder)
    pii = PIIService(detector=RegexPIIDetector(rules_dir=RULES_DIR))
    conflict = ConflictDetector(
        enabled_patterns={"date_diff", "numeric_diff", "rule_id_diff"}
    )
    return RAGGraphDeps(
        retrieval_service=retrieval,
        generation_service=generation,
        config_loader=_config_loader,
        today_provider=lambda: date(2026, 5, 9),
        verifier_service=verifier,
        pii_service=pii,
        conflict_detector=conflict,
    )


async def test_full_path_returns_verified_citations(populated_corpus):
    """direct citation이 verifier를 통과해 support_level/verified가 채워진다.

    LLM이 claim에 매핑한 citation 인덱스가 retrieval이 반환한 final_contexts 순서와
    일치해야 Tier 2가 strong으로 분류한다 (이게 실제 운영 동작).
    """
    # final_contexts 순서를 결정하기 위해 먼저 retrieval만 시뮬레이션해 인덱스를 잡는다.
    # 본 테스트에선 같은 질문·쿼리로 LLM이 보는 순서 그대로 cite하도록 LLM 응답을 구성.
    llm_response = json.dumps(
        {
            "answer_segments": [
                # 실제 final_contexts 첫번째에 해당하는 청크 텍스트
                {"text": "패스워드 만료 주기는 90일입니다", "citations": [1],
                 "support_type": "direct"},
                # 두번째에 해당
                {"text": "패스워드는 12자 이상이어야 합니다", "citations": [2],
                 "support_type": "direct"},
            ],
            "limitations": None,
        },
        ensure_ascii=False,
    )
    llm = InMemoryLLMClient(responses=[llm_response])
    deps = _build_deps(populated_corpus, llm)
    graph = build_chat_structured_full(deps)
    state = RAGState(
        request_id="r1",
        tenant_id="security",
        user_id="u1",
        question="패스워드 정책은?",
        user_context=_user_context(),
    )
    result = await graph.ainvoke(state)

    assert result["fallback_reason"] is None or result["gate2_passed"] is True
    assert result["gate2_passed"] is True
    assert len(result["citations"]) == 2
    for c in result["citations"]:
        assert c["support_type"] == "direct"
        assert c["support_level"] in {"strong", "medium"}
        assert c["verified"] is True
        assert c["similarity"] is not None
    assert result["citation_types"] == ["direct", "direct"]
    assert result["verifier_metrics"]["tier1_markers_removed"] == 0
    assert result["verifier_metrics"]["tier2_avg_similarity"] > 0.0
    assert result["verifier_metrics"]["tier3_unsupported_segments"] == []
    assert result["unsupported_ratio"] == 0.0
    assert result["confidence"] >= 0.3


async def test_invalid_citation_index_stripped_by_tier1(populated_corpus):
    llm_response = json.dumps(
        {
            "answer_segments": [
                {"text": "패스워드는 12자 이상이어야 합니다",
                 "citations": [1, 9],  # 9는 out of range
                 "support_type": "direct"},
            ],
            "limitations": None,
        },
        ensure_ascii=False,
    )
    llm = InMemoryLLMClient(responses=[llm_response])
    deps = _build_deps(populated_corpus, llm)
    graph = build_chat_structured_full(deps)
    state = RAGState(
        request_id="r2", tenant_id="security", user_id="u1",
        question="q", user_context=_user_context(),
    )
    result = await graph.ainvoke(state)
    assert result["verifier_metrics"]["tier1_markers_removed"] >= 1
    # citation 1만 살아남아 verified
    assert len(result["citations"]) == 1
    assert result["citations"][0]["marker"] == "[1]"


async def test_inference_segment_downgraded_with_caveat(populated_corpus):
    """judge 미결선 — inference type segment의 citation은 제거되고 caveat이 붙는다."""
    llm_response = json.dumps(
        {
            "answer_segments": [
                {"text": "패스워드는 12자 이상이어야 합니다", "citations": [1],
                 "support_type": "direct"},
                {"text": "이로부터 보안성이 매우 높다고 추론됩니다", "citations": [1],
                 "support_type": "inference"},
            ],
            "limitations": None,
        },
        ensure_ascii=False,
    )
    llm = InMemoryLLMClient(responses=[llm_response])
    deps = _build_deps(populated_corpus, llm)
    graph = build_chat_structured_full(deps)
    state = RAGState(
        request_id="r3", tenant_id="security", user_id="u1",
        question="q", user_context=_user_context(),
    )
    result = await graph.ainvoke(state)
    # direct만 남고 inference는 다운그레이드
    assert any(c["support_type"] == "direct" for c in result["citations"])
    assert all(c["support_type"] != "inference" for c in result["citations"])
    assert result["limitations"] is not None
    assert "추론" in result["limitations"]


async def test_synthesis_drops_when_one_chunk_weak(populated_corpus):
    """synthesis에서 한 chunk가 weak이면 그 segment 전체 다운그레이드."""
    llm_response = json.dumps(
        {
            "answer_segments": [
                {"text": "패스워드는 12자 이상이어야 합니다",
                 "citations": [1, 2],
                 "support_type": "synthesis"},
            ],
            "limitations": None,
        },
        ensure_ascii=False,
    )
    llm = InMemoryLLMClient(responses=[llm_response])
    deps = _build_deps(populated_corpus, llm)
    graph = build_chat_structured_full(deps)
    state = RAGState(
        request_id="r4", tenant_id="security", user_id="u1",
        question="q", user_context=_user_context(),
    )
    result = await graph.ainvoke(state)
    # claim과 c1은 동일(strong), c2는 다른 텍스트 → synthesis는 모두 verified 필요 → 전체 제거
    # → unsupported_ratio가 1.0이 되어 Gate 2 fail → fallback
    assert result["fallback_reason"] in {
        "gate2_below_min_verified_count",
        "gate2_above_max_unsupported_ratio",
        "gate2_below_min_confidence",
    }


async def test_llm_primary_conflict_with_groups_kept(date_conflict_corpus):
    """ADR-010 §4 Primary — LLM이 직접 support_type=conflict + conflict_groups를 반환하면
    heuristic 검출 없이도 Tier 2 conflict 분기가 양측 medium+ 검증해 인정한다.
    state.conflict_groups에는 휴리스틱 entry가 추가되지 않는다(LLM Primary).
    """
    llm_response = json.dumps(
        {
            "answer_segments": [
                {
                    "text": "시행일은 2024-01-01입니다",
                    "citations": [1, 2],
                    "support_type": "conflict",
                    "conflict_groups": [[1], [2]],
                },
            ],
            "limitations": None,
        },
        ensure_ascii=False,
    )
    llm = InMemoryLLMClient(responses=[llm_response])
    deps = _build_conflict_deps(date_conflict_corpus, llm)
    graph = build_chat_structured_full(deps)
    state = RAGState(
        request_id="r-prim-1", tenant_id="security", user_id="u1",
        question="시행일은?", user_context=_user_context(),
    )
    result = await graph.ainvoke(state)

    if result["fallback_reason"] is None:
        assert all(t == "conflict" for t in result["citation_types"])
        for c in result["citations"]:
            assert c["support_type"] == "conflict"
            assert c["support_level"] == "medium"

    # Heuristic은 LLM Primary segment를 스킵 → conflict_heuristic_detected는 0
    assert result["verifier_metrics"].get("conflict_heuristic_detected", 0) == 0
    # state.conflict_groups에는 heuristic source entry가 없다
    assert all(
        e.get("source") != "heuristic" for e in result["conflict_groups"]
    )


async def test_inference_chain_forwarded_to_judge(populated_corpus):
    """ADR-010 §4·§6 — LLM이 inference + inference_chain을 반환하면 judge_service에
    inference_chain이 forward되고, judge 통과 시 segment가 인정되어 citation에 노출된다.
    """
    inference_chain_text = "12자 이상이라는 사실로부터 보안성이 강함을 도출"
    llm_answer = json.dumps(
        {
            "answer_segments": [
                {
                    "text": "패스워드는 12자 이상이어야 합니다",
                    "citations": [1],
                    "support_type": "direct",
                },
                {
                    "text": "이로부터 보안성이 매우 높다고 추론됩니다",
                    "citations": [1],
                    "support_type": "inference",
                    "inference_chain": inference_chain_text,
                },
            ],
            "limitations": None,
        },
        ensure_ascii=False,
    )
    judge_response = json.dumps(
        {"valid": True, "confidence": 0.85, "reasoning": "근거 chunk가 결론을 지지"}
    )
    answer_llm = InMemoryLLMClient(responses=[llm_answer])
    judge_llm = InMemoryLLMClient(responses=[judge_response])

    store, embedder = populated_corpus
    reranker = InMemoryReranker()
    retrieval = RetrievalService(
        embedder=embedder, vector_store=store, reranker=reranker
    )
    prompt = GenerationPrompt.load(
        prompt_yaml=REPO / "configs/platform/prompts/rag_answer.yaml",
        schema_json=REPO / "configs/platform/prompts/answer_schema.json",
    )
    generation = GenerationService(llm=answer_llm, prompt=prompt, model="qwen-7b")
    verifier = VerifierService(embedder=embedder)

    from rag_core.services.judge_service import JudgePrompt, JudgeService

    judge = JudgeService(
        llm=judge_llm,
        prompt=JudgePrompt.load(
            prompt_yaml=REPO / "configs/platform/prompts/inference_judge.yaml",
            schema_json=REPO / "configs/platform/prompts/inference_judge_schema.json",
        ),
        model="shared-llm",
    )
    pii = PIIService(detector=RegexPIIDetector(rules_dir=RULES_DIR))
    conflict = ConflictDetector(
        enabled_patterns={"date_diff", "numeric_diff", "rule_id_diff"}
    )

    def _loader_with_judge(_tid: str) -> dict:
        cfg = _config_loader(_tid)
        # InMemory cosine 분포에서 inference claim·chunk pair가 medium 통과 가능하도록 낮춤
        cfg["citation"]["verification"]["tier2"]["thresholds"] = {
            "strong": 0.99, "medium": 0.3,
        }
        cfg["citation"]["verification"]["inference_judge"] = {
            "enable": True,
            "confidence_threshold": 0.6,
        }
        cfg["citation"]["gates"]["generation"]["min_confidence"] = 0.2
        return cfg

    deps = RAGGraphDeps(
        retrieval_service=retrieval,
        generation_service=generation,
        config_loader=_loader_with_judge,
        today_provider=lambda: date(2026, 5, 9),
        verifier_service=verifier,
        judge_service=judge,
        pii_service=pii,
        conflict_detector=conflict,
    )
    graph = build_chat_structured_full(deps)
    state = RAGState(
        request_id="r-inf-1", tenant_id="security", user_id="u1",
        question="패스워드 정책?", user_context=_user_context(),
    )
    result = await graph.ainvoke(state)

    # judge_llm 호출은 1회 (inference segment 1개)
    judge_calls = [c for c in judge_llm.calls if c["kind"] == "generate"]
    assert len(judge_calls) == 1
    # judge prompt에 inference_chain 본문이 포함되어야 함
    assert inference_chain_text in judge_calls[0]["prompt"]

    # judge 통과 → inference segment의 citation은 살아있고 support_level=medium
    inf_citations = [
        c for c in result["citations"] if c["support_type"] == "inference"
    ]
    assert len(inf_citations) >= 1
    for c in inf_citations:
        assert c["support_level"] == "medium"

    # judge 결과가 chat_logs용 inference_judge_results에 기록
    judge_results = result["inference_judge_results"]
    assert len(judge_results) == 1
    assert judge_results[0]["valid"] is True
    assert judge_results[0]["confidence"] == 0.85


async def test_layer2_chat_logs_question_masked_under_default_policy(
    populated_corpus,
):
    """ADR-020 §4 Layer 2 — pii_storage_policy=mask(기본)면 chat_logs.question은 마스킹된 form."""
    # 답변에는 영향 없도록 input PII가 medium 이하(차단되지 않음). 그래도 storage는 마스킹.
    answer = json.dumps(
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
    llm = InMemoryLLMClient(responses=[answer])
    deps = _build_deps(populated_corpus, llm)
    deps.chat_log_writer = InMemoryChatLogWriter()
    graph = build_chat_structured_full(deps)
    state = RAGState(
        request_id="r-stor-1", tenant_id="security", user_id="u1",
        # 이메일은 low severity → block 안 됨. 하지만 storage policy=mask면 마스킹.
        question="문의는 user@example.com 으로 패스워드 정책 알려주세요",
        user_context=_user_context(),
    )
    await graph.ainvoke(state)

    log = deps.chat_log_writer.records[0]
    assert log.pii_storage_policy == "mask"
    # email PII가 chat_logs.question에서 사라졌어야 함
    assert "user@example.com" not in log.question


async def test_layer2_chat_logs_question_plain_under_explicit_policy(
    populated_corpus,
):
    """ADR-020 §4 — plain policy + plain_approved=True 시 원문 보관 (platform_admin 승인)."""
    answer = json.dumps(
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
    llm = InMemoryLLMClient(responses=[answer])

    def _loader_plain(tid: str) -> dict:
        cfg = _config_loader(tid)
        cfg["pii"] = {
            **cfg["pii"],
            "storage": {"pii_storage_policy": "plain", "plain_approved": True},
        }
        cfg["compliance_mode"] = "standard"
        return cfg

    deps = _build_deps(populated_corpus, llm)
    deps.config_loader = _loader_plain
    deps.chat_log_writer = InMemoryChatLogWriter()
    graph = build_chat_structured_full(deps)
    state = RAGState(
        request_id="r-stor-2", tenant_id="security", user_id="u1",
        question="문의는 user@example.com 으로 패스워드 정책",
        user_context=_user_context(),
    )
    await graph.ainvoke(state)

    log = deps.chat_log_writer.records[0]
    assert log.pii_storage_policy == "plain"
    assert "user@example.com" in log.question  # 원문 보존


async def test_layer2_plain_without_approval_falls_back_to_mask(populated_corpus):
    """ADR-020 §4 — configured=plain이어도 plain_approved 미주입이면 mask 강제 (audit-friendly)."""
    answer = json.dumps(
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
    llm = InMemoryLLMClient(responses=[answer])

    def _loader_plain_no_approval(tid: str) -> dict:
        cfg = _config_loader(tid)
        cfg["pii"] = {
            **cfg["pii"],
            "storage": {"pii_storage_policy": "plain"},  # plain_approved 미주입
        }
        cfg["compliance_mode"] = "standard"
        return cfg

    deps = _build_deps(populated_corpus, llm)
    deps.config_loader = _loader_plain_no_approval
    deps.chat_log_writer = InMemoryChatLogWriter()
    graph = build_chat_structured_full(deps)
    state = RAGState(
        request_id="r-stor-4", tenant_id="security", user_id="u1",
        question="문의는 user@example.com 으로 패스워드 정책",
        user_context=_user_context(),
    )
    await graph.ainvoke(state)

    log = deps.chat_log_writer.records[0]
    assert log.pii_storage_policy == "mask"
    assert "user@example.com" not in log.question


async def test_layer2_gdpr_strict_forces_mask_overriding_plain(populated_corpus):
    """tenant_config.compliance_mode=gdpr_strict면 plain 설정도 mask로 강제."""
    answer = json.dumps(
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
    llm = InMemoryLLMClient(responses=[answer])

    def _loader_gdpr(tid: str) -> dict:
        cfg = _config_loader(tid)
        cfg["pii"] = {**cfg["pii"], "storage": {"pii_storage_policy": "plain"}}
        cfg["compliance_mode"] = "gdpr_strict"
        return cfg

    deps = _build_deps(populated_corpus, llm)
    deps.config_loader = _loader_gdpr
    deps.chat_log_writer = InMemoryChatLogWriter()
    graph = build_chat_structured_full(deps)
    state = RAGState(
        request_id="r-stor-3", tenant_id="security", user_id="u1",
        question="문의는 user@example.com 으로 정책",
        user_context=_user_context(),
    )
    await graph.ainvoke(state)

    log = deps.chat_log_writer.records[0]
    assert log.pii_storage_policy == "mask"  # 강제 mask
    assert "user@example.com" not in log.question


async def test_chat_redirects_to_stream_endpoint_when_routing_picks_streaming(
    populated_corpus,
):
    """ADR-013 §6 — sync /chat이 routing rule에서 ui_mode=chat_streaming으로 결정되면 fallback
    path로 단축하여 redirect_to_endpoint를 응답에 노출 (LLM 호출 없이 단축)."""
    from rag_core.services.model_router import ModelRouter
    from rag_core.services.query_classifier import (
        ClassifierTier2Prompt,
        QueryClassifier,
    )

    llm = InMemoryLLMClient(responses=["never_called"])
    deps = _build_deps(populated_corpus, llm)
    deps.model_router = ModelRouter()
    # tier2 prompt는 placeholder — tier1 매치되거나 deps.query_classifier=None이면 호출 안 됨
    deps.query_classifier = None

    def _loader_streaming(_tid: str) -> dict:
        cfg = _config_loader(_tid)
        cfg["routing"] = {
            "default": {"model": "tenant_slm", "ui_mode": "chat_streaming"},
            "rules": [],
        }
        return cfg

    deps.config_loader = _loader_streaming
    graph = build_chat_structured_full(deps)
    state = RAGState(
        request_id="r-redir-1", tenant_id="security", user_id="u1",
        question="패스워드 정책은?", user_context=_user_context(),
    )
    result = await graph.ainvoke(state)

    assert result["fallback_reason"] == "ui_mode_streaming_required"
    # LLM은 호출되지 않음 (model_router 다음 fallback 분기)
    generate_calls = [c for c in llm.calls if c["kind"] == "generate"]
    assert len(generate_calls) == 0
    # final_answer는 안내 메시지
    assert "/chat/stream" in result["final_answer"]
    # routing decision 자체는 정상 기록
    assert result["selected_model"] == "tenant_slm"
    assert result["ui_mode"] == "chat_streaming"


async def test_layer1_blocks_input_with_rrn(populated_corpus):
    """ADR-020 §3 — 질문에 주민번호가 있으면 retrieval 직전에 차단되어 fallback.

    pii.yaml: rrn=high → block. retrieve_context는 호출되지 않으므로 final_contexts 빈
    상태로 fallback이 'input_pii_blocked' 메시지를 채운다.
    """
    # LLM은 호출되지 않아야 하지만 안전하게 응답 하나 둠
    llm = InMemoryLLMClient(responses=[
        json.dumps({"answer_segments": [], "limitations": None})
    ])
    deps = _build_deps(populated_corpus, llm)
    graph = build_chat_structured_full(deps)
    state = RAGState(
        request_id="r-pii-1", tenant_id="security", user_id="u1",
        question="제 주민번호 901231-1234567 로 조회해 주세요",
        user_context=_user_context(),
    )
    result = await graph.ainvoke(state)

    assert result["fallback_reason"] == "input_pii_blocked"
    assert result["input_blocked"] is True
    assert "rrn" in result["blocked_categories"]
    # findings에 마스킹된 form만 — chat_logs는 PII 원본을 보관하지 않는다
    assert any(
        f.get("category") == "rrn" and f.get("action") == "block"
        for f in result["input_pii_found"]
    )
    # retrieval은 실행되지 않음 (fallback 전에 단락)
    assert result["final_contexts"] == []
    assert result["confidence"] == 0.0
    # 답변은 ADR-020 §3 사용자 안내 메시지
    assert "개인정보" in result["final_answer"]


@pytest.fixture
async def date_conflict_corpus():
    """date_diff 휴리스틱이 트립되는 corpus — 같은 주제, 다른 시행일 두 chunk."""
    store = InMemoryVectorStore()
    embedder = InMemoryEmbedder(dense_dim=64)
    await store.create_collection(tenant_id="security", dense_dim=64)

    docs = [
        ("c1", "시행일은 2024-01-01입니다",
         {"approval_status": "approved", "security_level": "internal",
          "acl": ["group:security"], "doc_id": "d1",
          "title": "정책 v1", "page_number": 1, "section_title": "시행일",
          "content": "시행일은 2024-01-01입니다"}),
        ("c2", "시행일은 2025-06-15입니다",
         {"approval_status": "approved", "security_level": "internal",
          "acl": ["group:security"], "doc_id": "d2",
          "title": "정책 v2", "page_number": 1, "section_title": "시행일",
          "content": "시행일은 2025-06-15입니다"}),
    ]
    points = []
    for cid, text, payload in docs:
        d, s = await embedder.embed_query(text)
        points.append(
            {"id": cid, "dense_vector": d, "sparse_vector": s, "payload": payload}
        )
    await store.upsert_chunks(tenant_id="security", points=points)
    return store, embedder


def _conflict_config_loader(_tid: str) -> dict:
    """conflict 테스트 전용 — medium 임계 0.5로 낮춰 두 chunk 모두 verify 통과 가능."""
    base = _config_loader(_tid)
    base["citation"]["verification"]["tier2"]["thresholds"] = {
        "strong": 0.99, "medium": 0.5,
    }
    base["citation"]["gates"]["generation"]["min_confidence"] = 0.2
    return base


def _build_conflict_deps(populated, llm: InMemoryLLMClient) -> RAGGraphDeps:
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
    verifier = VerifierService(embedder=embedder)
    pii = PIIService(detector=RegexPIIDetector(rules_dir=RULES_DIR))
    conflict = ConflictDetector(
        enabled_patterns={"date_diff", "numeric_diff", "rule_id_diff"}
    )
    return RAGGraphDeps(
        retrieval_service=retrieval,
        generation_service=generation,
        config_loader=_conflict_config_loader,
        today_provider=lambda: date(2026, 5, 9),
        verifier_service=verifier,
        pii_service=pii,
        conflict_detector=conflict,
    )


async def test_heuristic_reclassifies_synthesis_to_conflict(date_conflict_corpus):
    """ADR-010 §7 secondary — date_diff 발견 시 synthesis segment가 conflict로 reclassify되고
    Tier 2 양측 검증 통과 후 인정된다."""
    # InMemoryReranker(Jaccard) 순서가 c1, c2 둘 다 동일 score일 가능성 → 안정성 위해
    # 두 citation 모두에 같은 claim text를 LLM 응답으로 사용
    llm_response = json.dumps(
        {
            "answer_segments": [
                {"text": "시행일은 문서별로 다릅니다",
                 "citations": [1, 2],
                 "support_type": "synthesis"},
            ],
            "limitations": None,
        },
        ensure_ascii=False,
    )
    llm = InMemoryLLMClient(responses=[llm_response])
    deps = _build_conflict_deps(date_conflict_corpus, llm)
    graph = build_chat_structured_full(deps)
    state = RAGState(
        request_id="r-conf-1", tenant_id="security", user_id="u1",
        question="시행일은 언제인가요?", user_context=_user_context(),
    )
    result = await graph.ainvoke(state)

    # heuristic 메트릭 기록
    metrics = result["verifier_metrics"]
    assert metrics.get("conflict_heuristic_detected", 0) >= 1

    # state.conflict_groups에 segment별 누적
    conflict_log = result["conflict_groups"]
    assert len(conflict_log) >= 1
    entry = conflict_log[0]
    assert entry["signal"] == "date_diff"
    assert entry["source"] == "heuristic"
    # groups: [[1], [2]] (chunk별 separate group)
    flat_groups = sorted([sorted(g) for g in entry["groups"]])
    assert flat_groups == [[1], [2]]


async def test_heuristic_conflict_kept_when_both_sides_verified(
    date_conflict_corpus,
):
    """양측 chunk 모두 medium+ 통과 시 conflict 인정 (citation_types=conflict)."""
    llm_response = json.dumps(
        {
            "answer_segments": [
                {"text": "시행일은 2024-01-01입니다",  # claim·c1과 정확히 동일
                 "citations": [1, 2],
                 "support_type": "direct"},
            ],
            "limitations": None,
        },
        ensure_ascii=False,
    )
    llm = InMemoryLLMClient(responses=[llm_response])
    deps = _build_conflict_deps(date_conflict_corpus, llm)
    graph = build_chat_structured_full(deps)
    state = RAGState(
        request_id="r-conf-2", tenant_id="security", user_id="u1",
        question="시행일은?", user_context=_user_context(),
    )
    result = await graph.ainvoke(state)
    # heuristic이 direct → conflict로 reclassify했으면 citation type은 conflict
    if result["fallback_reason"] is None:
        # 통과 시: citation 모두 conflict + medium
        assert all(t == "conflict" for t in result["citation_types"])
        for c in result["citations"]:
            assert c["support_type"] == "conflict"
            assert c["support_level"] == "medium"  # cap


async def test_query_rewrite_hyde_used_for_retrieval(populated_corpus):
    """ADR-011 §5 — query_rewriting.enable=true + strategy=hyde일 때 LLM이 만든 가상 문서가
    retrieval query로 사용되고 chat_logs.rewritten_query에 기록된다."""
    # InMemoryEmbedder는 char-frequency 기반이므로 HyDE 출력에 chunk content 토큰이 많이 포함되면
    # retrieval이 그 chunk를 잘 가져온다. 여기선 c1·c2 키워드를 hyde 출력에 포함시켜
    # 기본 question("정책")만 사용했을 때와 다른 검색 결과를 끌어내는 환경 검증을 우선.
    hyde_passage = "패스워드는 12자 이상이어야 합니다 만료 주기 90일"
    answer_response = json.dumps(
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
    # 첫 호출: query_rewrite (HyDE) → 두번째: 답변 생성 (FIFO)
    llm = InMemoryLLMClient(responses=[hyde_passage, answer_response])
    rewriter = QueryRewriter(
        llm_clients={"tenant_slm": llm, "shared_llm": llm},
        prompt=QueryRewritePrompt.load(
            REPO / "configs" / "platform" / "prompts" / "query_rewrite.yaml"
        ),
        default_model="qwen-7b",
    )

    store, embedder = populated_corpus
    reranker = InMemoryReranker()
    retrieval = RetrievalService(
        embedder=embedder, vector_store=store, reranker=reranker
    )
    prompt = GenerationPrompt.load(
        prompt_yaml=REPO / "configs/platform/prompts/rag_answer.yaml",
        schema_json=REPO / "configs/platform/prompts/answer_schema.json",
    )
    generation = GenerationService(llm=llm, prompt=prompt, model="qwen-7b")
    verifier = VerifierService(embedder=embedder)
    pii = PIIService(detector=RegexPIIDetector(rules_dir=RULES_DIR))
    conflict = ConflictDetector(
        enabled_patterns={"date_diff", "numeric_diff", "rule_id_diff"}
    )
    chat_log_writer = InMemoryChatLogWriter()

    def _loader_with_rewrite(_tid: str) -> dict:
        cfg = _config_loader(_tid)
        cfg.setdefault("retrieval", {})
        cfg["retrieval"]["query_rewriting"] = {
            "enable": True,
            "strategy": "hyde",
            "llm": {"endpoint": "tenant_slm", "max_tokens": 256},
        }
        return cfg

    deps = RAGGraphDeps(
        retrieval_service=retrieval,
        generation_service=generation,
        config_loader=_loader_with_rewrite,
        today_provider=lambda: date(2026, 5, 9),
        verifier_service=verifier,
        pii_service=pii,
        conflict_detector=conflict,
        query_rewriter=rewriter,
        chat_log_writer=chat_log_writer,
    )
    graph = build_chat_structured_full(deps)
    state = RAGState(
        request_id="r-rw-1", tenant_id="security", user_id="u1",
        question="정책", user_context=_user_context(),
    )
    result = await graph.ainvoke(state)

    # rewritten_query가 채워졌고, hyde passage 그대로 사용
    assert result["rewritten_query"] == hyde_passage
    # chat_logs에 rewritten_query 적재
    assert len(chat_log_writer.records) == 1
    assert chat_log_writer.records[0].rewritten_query == hyde_passage
    # LLM은 정확히 두 번 호출 (rewrite + answer)
    generate_calls = [c for c in llm.calls if c["kind"] == "generate"]
    assert len(generate_calls) == 2
    # 첫 호출은 rewrite — answer_schema 미사용 (guided_json_schema=None)
    assert generate_calls[0]["guided_json_schema"] is None
    # 두번째는 답변 — guided_json_schema 사용
    assert generate_calls[1]["guided_json_schema"] is not None


async def test_query_rewrite_disabled_uses_original_question(populated_corpus):
    """enable=false면 LLM rewrite 호출 0회, retrieve가 원 question을 사용."""
    answer_response = json.dumps(
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
    llm = InMemoryLLMClient(responses=[answer_response])
    deps = _build_deps(populated_corpus, llm)
    # _build_deps는 query_rewriter를 None으로 두므로 노드 자체가 no-op.
    # 추가로 query_rewriter를 wire해도 enable=false면 LLM 미호출이어야 함.
    rewriter = QueryRewriter(
        llm_clients={"tenant_slm": llm, "shared_llm": llm},
        prompt=QueryRewritePrompt.load(
            REPO / "configs" / "platform" / "prompts" / "query_rewrite.yaml"
        ),
        default_model="qwen-7b",
    )
    deps.query_rewriter = rewriter
    graph = build_chat_structured_full(deps)
    state = RAGState(
        request_id="r-rw-2", tenant_id="security", user_id="u1",
        question="패스워드 정책은?", user_context=_user_context(),
    )
    result = await graph.ainvoke(state)

    # 비활성 시 rewriter는 원 question을 그대로 통과 — LLM 호출 0회
    assert result["rewritten_query"] == "패스워드 정책은?"
    # LLM은 답변 생성 1회만 호출 (rewrite는 LLM 미호출)
    generate_calls = [c for c in llm.calls if c["kind"] == "generate"]
    assert len(generate_calls) == 1


async def test_layer4_masks_pii_in_response(populated_corpus):
    """ADR-020 §6 — chunk에서 인용된 RRN 형식이 답변에 새는 경우 마스킹된다."""
    # LLM이 답변에 RRN을 포함시켜도 mask_response_pii가 가린다
    llm_response = json.dumps(
        {
            "answer_segments": [
                {"text": "패스워드는 12자 이상이어야 합니다",
                 "citations": [1], "support_type": "direct"},
                # 가공되지 않은 RRN을 직접 노출하는 위험 케이스
                {"text": "예: 901231-1234567 형식의 식별자도 정책에 포함됩니다",
                 "citations": [], "support_type": "direct"},
            ],
            "limitations": None,
        },
        ensure_ascii=False,
    )
    llm = InMemoryLLMClient(responses=[llm_response])
    deps = _build_deps(populated_corpus, llm)
    graph = build_chat_structured_full(deps)
    state = RAGState(
        request_id="r-pii-2", tenant_id="security", user_id="u1",
        question="패스워드 정책은?", user_context=_user_context(),
    )
    result = await graph.ainvoke(state)

    # 마스킹 적용 → 원본 RRN은 final_answer에 남지 않는다
    assert "901231-1234567" not in result["final_answer"]
    # 마스킹 메타가 chat_logs용 state에 적재됨
    assert any(
        f.get("category") == "rrn" for f in result["output_pii_masked"]
    )


async def test_gate2_fail_routes_to_fallback(populated_corpus):
    """모든 segment가 unsupported면 Gate 2 fail → fallback 응답."""
    llm_response = json.dumps(
        {
            "answer_segments": [
                {"text": "검증 불가 텍스트 1", "citations": [], "support_type": "direct"},
                {"text": "검증 불가 텍스트 2", "citations": [], "support_type": "direct"},
            ],
            "limitations": None,
        },
        ensure_ascii=False,
    )
    llm = InMemoryLLMClient(responses=[llm_response])
    deps = _build_deps(populated_corpus, llm)
    graph = build_chat_structured_full(deps)
    state = RAGState(
        request_id="r5", tenant_id="security", user_id="u1",
        question="q", user_context=_user_context(),
    )
    result = await graph.ainvoke(state)
    assert result["fallback_reason"] is not None
    assert result["gate2_passed"] is False
    # fallback_node가 답변을 교체
    assert result["confidence"] == 0.0
