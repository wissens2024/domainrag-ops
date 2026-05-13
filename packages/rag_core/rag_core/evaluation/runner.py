"""EvalRunner — chat_structured 그래프를 평가 케이스로 실행하고 metric을 계산.

흐름:
    for case in dataset.cases:
        deps.config_loader를 config_override 반영해 wrap →
        build_chat_structured_full(deps).ainvoke(RAGState) →
        case_result(case, state) → case별 metric →
    aggregate_metrics → EvaluationSummary

평가 자체는 LLM/Embedding을 실제 호출하므로 InMemory deps 또는 운영 deps 모두 가능.
운영자가 query_rewrite·conflict_primary·streaming 토글로 A/B를 돌릴 때는
case.config_override 또는 runner.run(extra_override=...)로 tenant_config 부분 override.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from rag_core.evaluation.dataset import EvalCase, EvalDataset
from rag_core.evaluation.metrics import (
    EvaluationSummary,
    aggregate_metrics,
    citation_accuracy,
    retrieval_recall_at_k,
    unsupported_ratio,
)
from rag_core.workflows import RAGGraphDeps, RAGState, build_chat_structured_full


@dataclass
class EvalCaseResult:
    """단일 케이스 실행 결과 — runner 내부 + (필요 시) 운영자 디버깅용."""

    case_id: str
    question: str
    tenant_id: str

    # 산출
    retrieved_chunk_ids: list[str] = field(default_factory=list)
    cited_chunk_ids: list[str] = field(default_factory=list)
    answer_segments: list[dict] = field(default_factory=list)
    fallback: bool = False
    fallback_reason: str | None = None

    # case 단위 metric (집계 입력)
    recall_at_5: float = 0.0
    citation_acc: float = 0.0
    unsupported: float = 0.0

    # 키워드 검증 + 기대 fallback 일치
    passed: bool = False
    failure_reasons: list[str] = field(default_factory=list)


class EvalRunner:
    """단일 RAGGraphDeps를 인자로 받아 dataset 전체를 평가.

    extra_override: 모든 case에 공통 적용할 tenant_config 부분 override (예: {"retrieval":
        {"query_rewrite": {"enable": False}}}).
    """

    def __init__(self, *, deps: RAGGraphDeps) -> None:
        self._deps = deps
        self._base_loader = deps.config_loader

    async def run(
        self,
        dataset: EvalDataset,
        *,
        extra_override: dict[str, Any] | None = None,
    ) -> tuple[EvaluationSummary, list[EvalCaseResult]]:
        per_case: list[EvalCaseResult] = []
        recalls: list[float] = []
        citations: list[float] = []
        unsupporteds: list[float] = []
        fallbacks: list[bool] = []
        passes: list[bool] = []

        for case in dataset.cases:
            result = await self._run_one(case, extra_override=extra_override)
            per_case.append(result)
            recalls.append(result.recall_at_5)
            citations.append(result.citation_acc)
            unsupporteds.append(result.unsupported)
            fallbacks.append(result.fallback)
            passes.append(result.passed)

        summary = aggregate_metrics(
            per_case_recall=recalls,
            per_case_citation=citations,
            per_case_unsupported=unsupporteds,
            per_case_fallback=fallbacks,
            per_case_pass=passes,
        )
        return summary, per_case

    async def _run_one(
        self,
        case: EvalCase,
        *,
        extra_override: dict[str, Any] | None = None,
    ) -> EvalCaseResult:
        merged_override = _merge(extra_override, case.config_override)

        async def _patched_loader(tid: str):
            cfg = self._base_loader(tid)
            if hasattr(cfg, "__await__"):
                cfg = await cfg  # type: ignore[assignment]
            cfg = copy.deepcopy(cfg or {})
            _deep_merge(cfg, merged_override)
            return cfg

        # graph 재생성은 비용 — config_loader만 swap하고 동일 인스턴스 재사용.
        original = self._deps.config_loader
        self._deps.config_loader = _patched_loader
        try:
            graph = build_chat_structured_full(self._deps)
            state = RAGState(
                request_id=f"eval-{case.case_id}",
                tenant_id=case.tenant_id,
                user_id=case.user_context.get("user_id", "eval-user"),
                question=case.question,
                user_context=case.user_context or {
                    "user_id": "eval-user",
                    "tenant_id": case.tenant_id,
                    "clearance": "internal",
                    "department": None,
                    "domain_groups": [],
                    "roles": ["USER"],
                },
                ui_mode=case.ui_mode,
            )
            out = await graph.ainvoke(state)
        finally:
            self._deps.config_loader = original

        return _build_case_result(case, out)


def _build_case_result(case: EvalCase, state: dict) -> EvalCaseResult:
    retrieved = [c.get("chunk_id") or c.get("id") for c in state.get("retrieved_chunks", [])]
    retrieved = [r for r in retrieved if r]

    cited = [c.get("chunk_id") for c in (state.get("citations") or [])]
    cited = [c for c in cited if c]

    answer_segments = state.get("answer_segments") or []
    fallback_reason = state.get("fallback_reason")
    is_fallback = bool(fallback_reason)

    recall = retrieval_recall_at_k(
        retrieved_chunk_ids=retrieved,
        expected_chunk_ids=case.expected_chunk_ids,
        k=5,
    )
    cite_acc = citation_accuracy(
        cited_chunk_ids=cited,
        expected_chunk_ids=case.expected_chunk_ids,
    )
    unsup = unsupported_ratio(answer_segments=answer_segments)

    answer_text = state.get("final_answer") or "".join(
        seg.get("text", "") for seg in answer_segments
    )

    failures: list[str] = []
    for kw in case.must_include_keywords:
        if kw not in answer_text:
            failures.append(f"missing_keyword:{kw}")
    for kw in case.must_not_include_keywords:
        if kw in answer_text:
            failures.append(f"forbidden_keyword:{kw}")
    if case.expected_fallback and not is_fallback:
        failures.append("expected_fallback_not_observed")
    if (not case.expected_fallback) and is_fallback:
        failures.append(f"unexpected_fallback:{fallback_reason}")

    return EvalCaseResult(
        case_id=case.case_id,
        question=case.question,
        tenant_id=case.tenant_id,
        retrieved_chunk_ids=list(retrieved),
        cited_chunk_ids=list(cited),
        answer_segments=list(answer_segments),
        fallback=is_fallback,
        fallback_reason=fallback_reason,
        recall_at_5=recall,
        citation_acc=cite_acc,
        unsupported=unsup,
        passed=not failures,
        failure_reasons=failures,
    )


def _merge(a: dict | None, b: dict | None) -> dict:
    """case override가 runner extra_override 위에 적용되도록 b가 우선."""
    out: dict = {}
    if a:
        _deep_merge(out, a)
    if b:
        _deep_merge(out, b)
    return out


def _deep_merge(base: dict, overlay: dict) -> None:
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
