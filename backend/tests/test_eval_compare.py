"""eval_compare CLI 단위 — _run_scenario / _format_markdown 검증 (시나리오 B 비교).

InMemory backend로 EvaluationOrchestrator를 호출해 두 번 run하고 markdown diff가 정상
빌드되는지 확인한다. mock LLM이 deterministic이라 A/B 차이는 config_override 효과로만
관찰 가능 — min_confidence를 다르게 줘서 fallback_rate 차이를 만든다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# tools 패키지가 backend가 아닌 repo root 하위에 있으므로 sys.path 보강
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.eval_compare import _format_markdown, _run_scenario  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.deps import (  # noqa: E402
    get_evaluation_orchestrator,
    reset_authfusion_token_client,
    reset_chat_log_eraser,
    reset_evaluation_orchestrator,
    reset_indexing_orchestrator,
    reset_oauth_state_store,
    reset_pii_approval_service,
    reset_rag_service,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_rag_service()
    reset_indexing_orchestrator()
    reset_pii_approval_service()
    reset_chat_log_eraser()
    reset_oauth_state_store()
    reset_authfusion_token_client()
    reset_evaluation_orchestrator()
    yield
    reset_rag_service()
    reset_indexing_orchestrator()
    reset_pii_approval_service()
    reset_chat_log_eraser()
    reset_oauth_state_store()
    reset_authfusion_token_client()
    reset_evaluation_orchestrator()


async def test_run_scenario_returns_summary_and_gate():
    """단일 _run_scenario 호출이 ScenarioRun(summary/gate_result/latency)를 반환."""
    settings = get_settings()
    orch = get_evaluation_orchestrator(settings)

    run = await _run_scenario(
        orch,
        tenant_id="security",
        dataset_name="tenant_security",
        label="A: baseline",
        config_override={},
    )
    assert run.label == "A: baseline"
    assert run.summary["total_cases"] == 5
    assert run.summary["retrieval_recall_at_5"] == 1.0
    assert "passed" in run.gate_result
    assert run.latency_seconds > 0


async def test_compare_two_scenarios_with_fallback_diff():
    """A: 기본, B: min_confidence=0.99 → B가 모든 case fallback. markdown에 diff 반영."""
    settings = get_settings()
    orch = get_evaluation_orchestrator(settings)

    run_a = await _run_scenario(
        orch,
        tenant_id="security",
        dataset_name="tenant_security",
        label="A: 7B-AWQ + 14B-AWQ",
        config_override={},
    )
    # B 실행 전 deps의 graph는 이미 컴파일되어 있으나 config_override는 매 case 적용되므로
    # 새 orchestrator 인스턴스 없이도 분리된 결과 확보 가능.
    run_b = await _run_scenario(
        orch,
        tenant_id="security",
        dataset_name="tenant_security",
        label="B: min_confidence raised",
        config_override={
            "citation": {"gates": {"generation": {"min_confidence": 0.99}}}
        },
    )

    assert run_a.summary["fallback_rate"] == 0.0
    assert run_b.summary["fallback_rate"] == 1.0

    report = _format_markdown(run_a, run_b)
    # 두 label 모두 보고서에 포함
    assert "A: 7B-AWQ + 14B-AWQ" in report
    assert "B: min_confidence raised" in report
    # diff 표가 fallback_rate +1.0000을 가져야 한다
    assert "fallback_rate" in report
    assert "+1.0000" in report
    # decision hint는 B가 더 안 좋으므로 "A 유지" 또는 "판단 보류" 라인
    assert "B 채택" not in report or "A 유지" in report


def test_format_markdown_handles_empty_gate_results():
    """gate_result가 빈 dict일 때도 markdown 빌드가 깨지지 않는다."""
    from tools.eval_compare import ScenarioRun

    a = ScenarioRun(
        label="A", job_id="EVAL-A",
        summary={
            "total_cases": 5, "retrieval_recall_at_5": 1.0,
            "citation_accuracy": 1.0, "unsupported_ratio": 0.0,
            "fallback_rate": 0.0, "pass_count": 5, "fail_count": 0,
        },
        gate_result={},
        latency_seconds=1.0,
        config_override={},
    )
    b = ScenarioRun(
        label="B", job_id="EVAL-B",
        summary={
            "total_cases": 5, "retrieval_recall_at_5": 0.8,
            "citation_accuracy": 0.85, "unsupported_ratio": 0.02,
            "fallback_rate": 0.0, "pass_count": 4, "fail_count": 1,
        },
        gate_result={},
        latency_seconds=0.6,
        config_override={},
    )
    out = _format_markdown(a, b)
    assert "Summary metrics" in out
    assert "wall-clock" in out.lower() or "Wall-clock" in out
    # latency Δ는 -0.40 (B가 빠름)
    assert "-0.40" in out
