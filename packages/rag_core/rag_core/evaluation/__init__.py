"""Evaluation harness (ADR-009 §7, ADR-017 §16).

평가셋 schema·loader·metrics·runner·promotion gate.

운영 디렉토리 구조:
    data/eval/
      platform/smoke.jsonl
      tenants/<tenant_id>/
        qa.jsonl
        citation_gold.jsonl
        promotion_gate.yaml
"""

from rag_core.evaluation.dataset import (
    EvalCase,
    EvalDataset,
    load_dataset,
    load_platform_smoke,
    load_tenant_dataset,
)
from rag_core.evaluation.metrics import (
    EvaluationSummary,
    aggregate_metrics,
    citation_accuracy,
    fallback_rate,
    retrieval_recall_at_k,
    unsupported_ratio,
)
from rag_core.evaluation.promotion_gate import (
    GateResult,
    PromotionGate,
    load_gate_yaml,
)
from rag_core.evaluation.runner import EvalCaseResult, EvalRunner

__all__ = [
    "EvalCase",
    "EvalDataset",
    "EvalCaseResult",
    "EvaluationSummary",
    "EvalRunner",
    "GateResult",
    "PromotionGate",
    "aggregate_metrics",
    "citation_accuracy",
    "fallback_rate",
    "load_dataset",
    "load_gate_yaml",
    "load_platform_smoke",
    "load_tenant_dataset",
    "retrieval_recall_at_k",
    "unsupported_ratio",
]
