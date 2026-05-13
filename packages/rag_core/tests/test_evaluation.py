"""평가 harness — dataset loader / metrics / promotion gate / EvalRunner e2e.

InMemory deps를 그대로 활용해 평가 흐름이 chat_structured_full 그래프를 정상 호출하고
운영 가능한 metric을 반환하는지 검증한다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rag_core.clients.in_memory import InMemoryLLMClient
from rag_core.evaluation import (
    EvalCase,
    EvalDataset,
    EvalRunner,
    PromotionGate,
    aggregate_metrics,
    citation_accuracy,
    fallback_rate,
    load_dataset,
    load_gate_yaml,
    load_platform_smoke,
    load_tenant_dataset,
    retrieval_recall_at_k,
    unsupported_ratio,
)
from rag_core.services.chat_log_writer import InMemoryChatLogWriter
from rag_core.services.judge_service import JudgePrompt, JudgeService

# 본 모듈은 test_chat_structured_full의 fixture·helper를 재활용한다.
from tests.test_chat_structured_full import (  # type: ignore[import-not-found]
    REPO,
    _build_deps,
    _config_loader,
    populated_corpus,  # noqa: F401 — pytest fixture import
)


EVAL_ROOT = REPO / "data" / "eval"


# --------------------------------------------------------------------------- #
# unit — metrics
# --------------------------------------------------------------------------- #


def test_retrieval_recall_at_k_basic() -> None:
    assert (
        retrieval_recall_at_k(
            retrieved_chunk_ids=["c1", "c2", "c3"],
            expected_chunk_ids=["c1", "c2"],
            k=5,
        )
        == 1.0
    )
    # k=1로 자르면 c2 miss
    assert (
        retrieval_recall_at_k(
            retrieved_chunk_ids=["c1", "c2", "c3"],
            expected_chunk_ids=["c1", "c2"],
            k=1,
        )
        == 0.5
    )
    # expected가 비어있으면 평가 제외 → 1.0
    assert retrieval_recall_at_k(retrieved_chunk_ids=[], expected_chunk_ids=[]) == 1.0


def test_citation_accuracy_basic() -> None:
    assert (
        citation_accuracy(
            cited_chunk_ids=["c1", "c2"],
            expected_chunk_ids=["c1", "c2"],
        )
        == 1.0
    )
    # 잘못된 citation 1건 섞임 → 0.5
    assert (
        citation_accuracy(
            cited_chunk_ids=["c1", "cX"],
            expected_chunk_ids=["c1", "c2"],
        )
        == 0.5
    )
    # citation 없으면 0
    assert citation_accuracy(cited_chunk_ids=[], expected_chunk_ids=["c1"]) == 0.0
    # expected 없으면 1
    assert citation_accuracy(cited_chunk_ids=["c1"], expected_chunk_ids=[]) == 1.0


def test_unsupported_ratio_basic() -> None:
    segs = [
        {"text": "a", "support_type": "direct"},
        {"text": "b", "support_type": "unsupported"},
    ]
    assert unsupported_ratio(answer_segments=segs) == 0.5
    assert unsupported_ratio(answer_segments=[]) == 0.0


def test_fallback_rate_basic() -> None:
    assert fallback_rate([False, False, True, False]) == 0.25
    assert fallback_rate([]) == 0.0


# --------------------------------------------------------------------------- #
# dataset loader
# --------------------------------------------------------------------------- #


def test_platform_smoke_loader() -> None:
    ds = load_platform_smoke(EVAL_ROOT)
    assert ds.name == "platform_smoke"
    assert len(ds.cases) >= 5
    assert ds.cases[0].case_id == "smoke-001"
    # citation_gold 없음 → expected_chunk_ids 빈 리스트
    assert ds.cases[0].expected_chunk_ids == []


def test_tenant_dataset_joins_citation_gold() -> None:
    ds = load_tenant_dataset(EVAL_ROOT, "security")
    assert ds.tenant_id == "security"
    assert len(ds.cases) == 5
    by_id = {c.case_id: c for c in ds.cases}
    assert by_id["sec-qa-001"].expected_chunk_ids == ["c1", "c2"]
    assert by_id["sec-qa-002"].expected_chunk_ids == ["c2"]


# --------------------------------------------------------------------------- #
# promotion gate
# --------------------------------------------------------------------------- #


def test_promotion_gate_load_yaml() -> None:
    gate = load_gate_yaml(EVAL_ROOT / "tenants" / "security" / "promotion_gate.yaml")
    assert gate.auto_promote is False
    assert "retrieval_recall_at_5" in gate.metrics_cfg
    assert gate.metrics_cfg["citation_accuracy"]["min"] == 0.90


def test_promotion_gate_pass_and_fail() -> None:
    gate = PromotionGate(
        metrics={
            "retrieval_recall_at_5": {"min": 0.80},
            "unsupported_ratio": {"max": 0.10},
        }
    )
    summary = aggregate_metrics(
        per_case_recall=[1.0, 1.0],
        per_case_citation=[1.0, 1.0],
        per_case_unsupported=[0.0, 0.0],
        per_case_fallback=[False, False],
        per_case_pass=[True, True],
    )
    result = gate.evaluate(summary)
    assert result.passed is True

    # recall 미달
    bad = aggregate_metrics(
        per_case_recall=[0.5, 0.5],
        per_case_citation=[1.0, 1.0],
        per_case_unsupported=[0.0, 0.0],
        per_case_fallback=[False, False],
        per_case_pass=[True, True],
    )
    bad_result = gate.evaluate(bad)
    assert bad_result.passed is False
    bad_recall = next(m for m in bad_result.metrics if m.name == "retrieval_recall_at_5")
    assert bad_recall.passed is False


# --------------------------------------------------------------------------- #
# EvalRunner e2e — InMemory deps로 chat_structured_full 그래프 호출
# --------------------------------------------------------------------------- #


def _fixed_llm_response() -> str:
    """test_chat_structured_full와 동일한 mock 응답 — c2 → c1 순으로 citation."""
    return json.dumps(
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


async def test_eval_runner_executes_dataset_and_aggregates_metrics(populated_corpus):
    """실 corpus + mock LLM으로 chat_structured_full을 흘려보내고 recall/citation_accuracy를
    집계한다. mock LLM이 같은 응답을 반환하므로 모든 case의 citation은 c1·c2."""
    llm = InMemoryLLMClient(responses=[_fixed_llm_response()] * 5)
    deps = _build_deps(populated_corpus, llm)
    deps.chat_log_writer = InMemoryChatLogWriter()

    # judge 비활성 — 4-type primary 흐름은 별도 검증 모듈 책임. 본 e2e는 retrieval/cite 위주.
    deps.judge_service = None  # type: ignore[assignment]

    # security tenant 평가셋 (5건, expected_chunk_ids c1/c2)
    ds = load_tenant_dataset(EVAL_ROOT, "security")
    summary, per_case = await EvalRunner(deps=deps).run(ds)

    assert summary.total_cases == 5
    # 모든 case가 c1·c2를 retrieve → recall@5 = 1.0
    assert summary.retrieval_recall_at_5 == 1.0
    # mock LLM이 모든 case에 c1·c2를 cite. expected가 [c2] 또는 [c1]인 케이스는
    # 추가 citation으로 precision 0.5 → 5건 평균 (1+0.5+0.5+1+1)/5 = 0.8.
    assert summary.citation_accuracy == pytest.approx(0.8)
    # mock LLM 응답에 unsupported segment 없음
    assert summary.unsupported_ratio == 0.0
    # fallback 없음
    assert summary.fallback_rate == 0.0

    # case별 passed 검증 — must_include 키워드('12','90')도 응답에 포함됨
    for case_result in per_case:
        assert case_result.passed, case_result.failure_reasons


async def test_promotion_gate_against_runner_summary(populated_corpus):
    """runner 산출 summary로 promotion_gate를 평가 — security gate(citation>=0.90)는
    mock LLM extra citation 때문에 실패해야 한다(=실패 흐름의 의미를 검증)."""
    llm = InMemoryLLMClient(responses=[_fixed_llm_response()] * 5)
    deps = _build_deps(populated_corpus, llm)
    deps.chat_log_writer = InMemoryChatLogWriter()
    deps.judge_service = None  # type: ignore[assignment]
    ds = load_tenant_dataset(EVAL_ROOT, "security")
    summary, _ = await EvalRunner(deps=deps).run(ds)

    gate = load_gate_yaml(EVAL_ROOT / "tenants" / "security" / "promotion_gate.yaml")
    result = gate.evaluate(summary)
    assert result.passed is False
    cite_metric = next(m for m in result.metrics if m.name == "citation_accuracy")
    assert cite_metric.passed is False
    recall_metric = next(m for m in result.metrics if m.name == "retrieval_recall_at_5")
    assert recall_metric.passed is True


async def test_eval_runner_extra_override_applies_to_all_cases(populated_corpus):
    """extra_override가 tenant_config에 deep-merge되어 모든 case에 적용되는지 검증.

    citation.gates.generation.min_confidence를 임의로 0.99로 올리면 Gate 2 실패 → fallback.
    Gate 통과 도메인 default(0.3) 대비 정책 토글 효과를 명확히 검증할 수 있다.
    """
    llm = InMemoryLLMClient(responses=[_fixed_llm_response()] * 5)
    deps = _build_deps(populated_corpus, llm)
    deps.chat_log_writer = InMemoryChatLogWriter()
    deps.judge_service = None  # type: ignore[assignment]

    ds = load_tenant_dataset(EVAL_ROOT, "security")
    override = {
        "citation": {"gates": {"generation": {"min_confidence": 0.99}}}
    }
    summary, per_case = await EvalRunner(deps=deps).run(ds, extra_override=override)

    # min_confidence 0.99 — InMemory에서 confidence는 그 이하이므로 모든 case가 fallback
    assert summary.fallback_rate == 1.0
    assert all(c.fallback for c in per_case)
