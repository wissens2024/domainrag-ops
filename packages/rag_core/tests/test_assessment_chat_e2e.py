"""ADR-027 — 대화형 출제 e2e.

채팅에서 "출제" 의도가 assessment_node로 분기되어 문제은행 근거 generate(persist=False)
로 문항을 만들고 grounding='assessment'로 응답하는지 검증한다.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import yaml

from rag_core.clients.in_memory import (
    InMemoryEmbedder,
    InMemoryLLMClient,
    InMemoryReranker,
    InMemoryVectorStore,
)
from rag_core.interfaces.assessment_item_repository import AssessmentItemRecord
from rag_core.services.assessment_generate import GenerateCriteria, GenerateResult
from rag_core.services.chat_log_writer import InMemoryChatLogWriter
from rag_core.services.generation_service import GenerationPrompt, GenerationService
from rag_core.services.model_router import ModelRouter
from rag_core.services.query_classifier import QueryClassifier
from rag_core.services.retrieval_service import RetrievalService
from rag_core.services.verifier_service import VerifierService
from rag_core.workflows import RAGGraphDeps, RAGState, build_chat_structured_full

REPO = Path(__file__).resolve().parents[3]


class _FakeAssessmentGen:
    """generate(persist=False) 호출을 기록하고 subject·count대로 문항 반환."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def generate(self, *, domain_id, criteria: GenerateCriteria,
                       actor=None, validators_config=None, persist=True):
        self.calls.append({"subject": criteria.subject, "count": criteria.count,
                           "persist": persist})
        items = [
            AssessmentItemRecord(
                item_id=f"Q-{criteria.subject}-{i}",
                domain_id=domain_id,
                subject=criteria.subject,
                difficulty=criteria.difficulty,
                question_text=f"{criteria.subject} 신규 문제 {i}",
                choices=["A", "B", "C", "D"],
                answer="A",
                explanation="해설",
                source="generated",
            )
            for i in range(1, criteria.count + 1)
        ]
        return GenerateResult(items=items, generated_count=len(items))


class _FakeItemRepo:
    def __init__(self, by_subject: dict[str, int]) -> None:
        self._by_subject = by_subject

    async def analytics_summary(self, *, domain_id):
        return {"by_subject": dict(self._by_subject)}


def _user() -> dict:
    return {"user_id": "u1", "domain_id": "security", "clearance": "internal",
            "department": "x", "domain_groups": [], "roles": ["USER"]}


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
        "query_classifier": yaml.safe_load(
            (REPO / "configs/platform/query_classifier.yaml").read_text(encoding="utf-8")
        ),
        "routing": yaml.safe_load(
            (REPO / "configs/platform/routing.yaml").read_text(encoding="utf-8")
        ),
        "model": {
            "tenant_slm": {"endpoint": "vllm-tenant", "lora_adapter": "x"},
            "shared_llm": {"endpoint": "vllm-shared", "lora_adapter": None},
        },
    }


class _FakeFigureReuse:
    """figure-reuse 서비스 — assets(storage_key) 보유 문항 반환."""

    def __init__(self, *, vlm_unavailable=False, subjects_with_figs=("database",),
                 max_per_subject=99) -> None:
        self.vlm_unavailable = vlm_unavailable
        self._subjects = set(subjects_with_figs)
        self._cap = max_per_subject
        self.calls = []

    async def generate(self, *, domain_id, criteria):
        from rag_core.services.assessment_figure_reuse import FigureReuseResult

        self.calls.append(criteria.subject)
        if self.vlm_unavailable:
            return FigureReuseResult(vlm_unavailable=True)
        if criteria.subject not in self._subjects:
            return FigureReuseResult(skipped_no_image=1)
        n = min(criteria.count, self._cap)  # 과목 그림 부족 시뮬레이션
        items = [
            AssessmentItemRecord(
                item_id=f"FQ-{i}", domain_id=domain_id, subject=criteria.subject,
                difficulty=criteria.difficulty,
                question_text="다음 그림의 트리 후위 순회 결과는?",
                choices=["A", "B", "C", "D"], answer="A", explanation="해설",
                source="generated", figure_dependent=True,
                assets=[{"asset_id": "a1",
                         "storage_key": f"items/{domain_id}/ref/a1.png"}],
            )
            for i in range(1, n + 1)
        ]
        return FigureReuseResult(items=items, generated_count=len(items),
                                 references_used=["ref"])


async def _build_deps(*, assessment_gen, item_repo, figure_reuse=None) -> RAGGraphDeps:
    store = InMemoryVectorStore()
    embedder = InMemoryEmbedder(dense_dim=64)
    await store.create_collection(domain_id="security", dense_dim=64)
    retrieval = RetrievalService(
        embedder=embedder, vector_store=store, reranker=InMemoryReranker()
    )
    prompt = GenerationPrompt.load(
        prompt_yaml=REPO / "configs/platform/prompts/rag_answer.yaml",
        schema_json=REPO / "configs/platform/prompts/answer_schema.json",
    )
    generation = GenerationService(
        llm=InMemoryLLMClient(responses=["unused"]), prompt=prompt, model="tenant_slm"
    )
    classifier_prompt = QueryClassifier.load_tier2_prompt(
        REPO / "configs/platform/prompts/query_classifier_tier2.yaml"
    )
    classifier = QueryClassifier(
        llm=InMemoryLLMClient(responses=["unused"]), prompt=classifier_prompt,
        model="tenant_slm",
    )
    return RAGGraphDeps(
        retrieval_service=retrieval,
        generation_service=generation,
        config_loader=_config_loader,
        today_provider=lambda: date(2026, 6, 19),
        verifier_service=VerifierService(embedder=embedder),
        chat_log_writer=InMemoryChatLogWriter(),
        query_classifier=classifier,
        model_router=ModelRouter(),
        assessment_generate_service=assessment_gen,
        assessment_item_repo=item_repo,
        assessment_figure_reuse_service=figure_reuse,
    )


async def test_chat_specific_subject_generates_without_persist():
    gen = _FakeAssessmentGen()
    repo = _FakeItemRepo({"security": 100, "database": 50})
    deps = await _build_deps(assessment_gen=gen, item_repo=repo)
    graph = build_chat_structured_full(deps)
    state = RAGState(request_id="r1", domain_id="security", user_id="u1",
                     question="정보보안 2문제 출제해줘", user_context=_user())
    result = await graph.ainvoke(state)

    assert result["query_type"] == "assessment_generation"
    assert result["grounding"] == "assessment"
    items = result["assessment_items"]
    assert len(items) == 2
    assert all(it["subject"] == "security" for it in items)
    # persist=False (ephemeral) 로 호출됐다.
    assert gen.calls and all(c["persist"] is False for c in gen.calls)
    # chat_logs에 저장되고 routing/grounding 반영.
    rec = deps.chat_log_writer.records[0]
    assert rec.routing_decision["grounding"] == "assessment"


async def test_chat_per_subject_distributes_and_caps():
    gen = _FakeAssessmentGen()
    repo = _FakeItemRepo({f"subj_{i}": 100 for i in range(10)})
    deps = await _build_deps(assessment_gen=gen, item_repo=repo)
    graph = build_chat_structured_full(deps)
    state = RAGState(request_id="r2", domain_id="security", user_id="u1",
                     question="과목별로 2문제씩 출제해줘", user_context=_user())
    result = await graph.ainvoke(state)

    assert result["grounding"] == "assessment"
    # 과목별 2문제, max_subjects(6)·max_total(20) cap 적용 → 6과목 × 2 = 12문항.
    assert len(result["assessment_items"]) == 12
    assert len({c["subject"] for c in gen.calls}) == 6


async def test_non_assessment_question_uses_normal_rag():
    gen = _FakeAssessmentGen()
    repo = _FakeItemRepo({"security": 100})
    deps = await _build_deps(assessment_gen=gen, item_repo=repo)
    graph = build_chat_structured_full(deps)
    state = RAGState(request_id="r3", domain_id="security", user_id="u1",
                     question="패스워드 정책이 어떻게 되나요?", user_context=_user())
    result = await graph.ainvoke(state)

    # 출제 의도가 아니므로 assessment 분기를 타지 않는다.
    assert result["query_type"] != "assessment_generation"
    assert result["grounding"] != "assessment"
    assert gen.calls == []


async def test_chat_figure_question_routes_to_figure_reuse_with_image():
    """ADR-027 — '그림 문제' 의도면 figure-reuse 경로 + image_url 포함."""
    gen = _FakeAssessmentGen()
    repo = _FakeItemRepo({"database": 100, "operating_system": 50})
    fig = _FakeFigureReuse(subjects_with_figs=("database",))
    deps = await _build_deps(assessment_gen=gen, item_repo=repo, figure_reuse=fig)
    graph = build_chat_structured_full(deps)
    state = RAGState(request_id="r4", domain_id="security", user_id="u1",
                     question="그림 문제 하나 출제해줘", user_context=_user())
    result = await graph.ainvoke(state)

    assert result["grounding"] == "assessment"
    items = result["assessment_items"]
    assert len(items) == 1
    # 그림 이미지 서빙 URL이 포함된다.
    assert items[0]["image_url"].startswith("/api/security/assessment/asset?key=")
    assert "items%2Fsecurity%2Fref%2Fa1.png" in items[0]["image_url"]
    # figure-reuse 서비스가 호출되고, 텍스트 generate는 안 탄다.
    assert fig.calls and gen.calls == []


async def test_chat_figure_named_subject_no_substitution():
    """ADR-027 — 명시 과목에 그림이 없으면 다른 과목으로 대체하지 않고 안내한다."""
    gen = _FakeAssessmentGen()
    repo = _FakeItemRepo({"operating_system": 100, "database": 50})
    fig = _FakeFigureReuse(subjects_with_figs=("database",))  # OS엔 그림 없음
    deps = await _build_deps(assessment_gen=gen, item_repo=repo, figure_reuse=fig)
    graph = build_chat_structured_full(deps)
    state = RAGState(request_id="r6", domain_id="security", user_id="u1",
                     question="운영체제에 그림이 들어간 문제 2개만 출제해줘",
                     user_context=_user())
    result = await graph.ainvoke(state)

    # database로 대체하지 않는다 — figure-reuse는 operating_system만 호출.
    assert fig.calls == ["operating_system"]
    assert result["grounding"] == "ungrounded"
    assert "operating_system" in result["final_answer"]
    assert not result["assessment_items"]


async def test_chat_figure_named_subject_shortfall_notes():
    """ADR-027 — 명시 과목 그림이 요청보다 적으면 있는 만큼만 + 부족 안내."""
    gen = _FakeAssessmentGen()
    repo = _FakeItemRepo({"operating_system": 100, "database": 50})
    fig = _FakeFigureReuse(subjects_with_figs=("operating_system",), max_per_subject=1)
    deps = await _build_deps(assessment_gen=gen, item_repo=repo, figure_reuse=fig)
    graph = build_chat_structured_full(deps)
    state = RAGState(request_id="r7", domain_id="security", user_id="u1",
                     question="운영체제 그림 문제 2개 출제해줘", user_context=_user())
    result = await graph.ainvoke(state)

    assert fig.calls == ["operating_system"]  # 다른 과목 대체 없음
    assert len(result["assessment_items"]) == 1  # 있는 만큼만
    assert "1개만 출제" in result["final_answer"]


async def test_chat_figure_question_degrades_when_vlm_down():
    """ADR-027 — VLM 비가동이면 그림 출제 안내(ungrounded)."""
    gen = _FakeAssessmentGen()
    repo = _FakeItemRepo({"database": 100})
    fig = _FakeFigureReuse(vlm_unavailable=True)
    deps = await _build_deps(assessment_gen=gen, item_repo=repo, figure_reuse=fig)
    graph = build_chat_structured_full(deps)
    state = RAGState(request_id="r5", domain_id="security", user_id="u1",
                     question="그림 문제 출제해줘", user_context=_user())
    result = await graph.ainvoke(state)

    assert result["grounding"] == "ungrounded"
    assert "VLM" in result["final_answer"]
