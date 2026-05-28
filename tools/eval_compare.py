"""eval_compare — 두 시나리오의 평가 결과를 나란히 실행·비교 (ADR-019 시나리오 A/B 결정 도구).

사용 예시:
    poetry run python -m tools.eval_compare \\
        --tenant security \\
        --dataset tenant_security \\
        --label-a "A: 7B-AWQ + 14B-AWQ" \\
        --label-b "B: 7B fp16 단일"

본 CLI는 두 시나리오에 대응되는 model.yaml(또는 config_override)을 차례로 적용해
EvaluationOrchestrator를 호출하고, 결과 summary + gate 통과 여부 + latency를 markdown
표로 출력한다.

실제 시나리오 차이는 endpoint를 가리키는 env var(TENANT_SLM_BASE_URL_A vs _B)이므로,
운영자는 본 도구를 호출하기 전에 두 vLLM 인스턴스를 띄워두거나, 단일 endpoint를
바라보게 한 뒤 config_override로 base_model/temperature 등 차이만 줘도 된다.

InMemory backend(RAG_BACKEND=inmemory)에서는 deterministic mock LLM이라 두 시나리오의
응답이 동일 — 본 CLI 자체의 동작 검증(diff 표 빌드)만 가능하다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ScenarioRun:
    label: str
    job_id: str
    summary: dict[str, Any]
    gate_result: dict[str, Any]
    latency_seconds: float
    config_override: dict[str, Any]


async def _run_scenario(
    orchestrator,
    *,
    domain_id: str,
    dataset_name: str,
    label: str,
    config_override: dict[str, Any],
    actor: str = "eval-compare-cli",
) -> ScenarioRun:
    """한 시나리오에 대해 evaluation을 실행하고 완료까지 대기 후 결과 반환."""
    started = time.perf_counter()
    prepared = await orchestrator.prepare_run(
        domain_id=domain_id,
        dataset_name=dataset_name,
        actor=actor,
        config_override=config_override,
    )
    # background 의존 없이 직접 execute (caller가 await — 완료까지 sync 진행)
    await orchestrator.execute(job_id=prepared.job_id, domain_id=domain_id)
    elapsed = time.perf_counter() - started

    record = await orchestrator.repo.get(
        domain_id=domain_id, job_id=prepared.job_id
    )
    return ScenarioRun(
        label=label,
        job_id=prepared.job_id,
        summary=dict(record.summary or {}),
        gate_result=dict(record.gate_result or {}),
        latency_seconds=elapsed,
        config_override=config_override,
    )


def _format_markdown(a: ScenarioRun, b: ScenarioRun) -> str:
    """A vs B 결과를 markdown 표로 렌더링."""
    metrics = [
        "total_cases",
        "retrieval_recall_at_5",
        "citation_accuracy",
        "unsupported_ratio",
        "fallback_rate",
        "pass_count",
        "fail_count",
    ]

    lines = []
    lines.append(f"# Evaluation Compare — {a.label} vs {b.label}")
    lines.append("")
    lines.append("## Summary metrics")
    lines.append("")
    lines.append("| Metric | A | B | Δ (B − A) |")
    lines.append("|---|---:|---:|---:|")
    for m in metrics:
        av = a.summary.get(m, 0)
        bv = b.summary.get(m, 0)
        if isinstance(av, (int, float)) and isinstance(bv, (int, float)):
            delta = bv - av
            lines.append(f"| {m} | {av:.4f} | {bv:.4f} | {delta:+.4f}" if isinstance(av, float)
                         else f"| {m} | {av} | {bv} | {delta:+d}")
        else:
            lines.append(f"| {m} | {av} | {bv} | - |")

    lines.append("")
    lines.append("## Gate result")
    lines.append("")
    lines.append("| 항목 | A | B |")
    lines.append("|---|---|---|")
    lines.append(f"| passed | {a.gate_result.get('passed')} | {b.gate_result.get('passed')} |")
    a_failed = [
        m["name"] for m in a.gate_result.get("metrics", []) if not m.get("passed")
    ]
    b_failed = [
        m["name"] for m in b.gate_result.get("metrics", []) if not m.get("passed")
    ]
    lines.append(f"| failed metrics | {', '.join(a_failed) or '-'} | {', '.join(b_failed) or '-'} |")
    lines.append("")
    lines.append("## Wall-clock latency")
    lines.append("")
    lines.append(f"- A: {a.latency_seconds:.2f}s ({a.job_id})")
    lines.append(f"- B: {b.latency_seconds:.2f}s ({b.job_id})")
    lines.append(f"- Δ: {(b.latency_seconds - a.latency_seconds):+.2f}s")
    lines.append("")
    lines.append("## Decision hint")
    lines.append("")
    fallback_diff = (
        b.summary.get("fallback_rate", 0) - a.summary.get("fallback_rate", 0)
    )
    citation_diff = (
        b.summary.get("citation_accuracy", 0) - a.summary.get("citation_accuracy", 0)
    )
    if fallback_diff <= 0.05 and citation_diff >= -0.05 and b.latency_seconds < a.latency_seconds:
        lines.append(
            "**B 채택 권장** — fallback_rate 증가 ≤ 5%p, citation_accuracy 손실 ≤ 5%p, "
            "latency 단축. 모델 weight 단일화 효과 확보."
        )
    elif b.gate_result.get("passed") and not a.gate_result.get("passed"):
        lines.append("**B 채택 권장** — A는 gate 미통과인데 B는 통과.")
    elif a.gate_result.get("passed") and not b.gate_result.get("passed"):
        lines.append("**A 유지** — B는 gate 미통과.")
    else:
        lines.append(
            "**판단 보류** — 위 metric을 도메인 우선순위에 따라 비교해 운영자가 결정."
        )
    return "\n".join(lines)


async def _amain(args) -> int:
    # 본 import는 runtime에 — backend 패키지 의존을 import 시점에 강제하지 않도록.
    import sys
    sys.path.insert(0, str(Path(args.backend_root).resolve()))

    from app.core.config import get_settings
    from app.deps import get_evaluation_orchestrator, reset_evaluation_orchestrator

    settings = get_settings()
    reset_evaluation_orchestrator()
    orch = get_evaluation_orchestrator(settings)

    override_a = json.loads(args.override_a) if args.override_a else {}
    override_b = json.loads(args.override_b) if args.override_b else {}

    run_a = await _run_scenario(
        orch,
        domain_id=args.tenant,
        dataset_name=args.dataset,
        label=args.label_a,
        config_override=override_a,
    )
    # B 실행 시 deps의 config가 다른 endpoint를 가리키게 하려면 운영 환경 변수
    # (TENANT_SLM_BASE_URL_B, SHARED_LLM_BASE_URL_B)가 활성화되어 있어야 한다.
    run_b = await _run_scenario(
        orch,
        domain_id=args.tenant,
        dataset_name=args.dataset,
        label=args.label_b,
        config_override=override_b,
    )

    report = _format_markdown(run_a, run_b)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
    # Windows cp949 console에서도 안전하게 출력
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # py3.7+
    except AttributeError:
        pass
    print(report)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluation A vs B compare")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--label-a", default="A")
    parser.add_argument("--label-b", default="B")
    parser.add_argument(
        "--override-a", default="",
        help="A 시나리오 config_override JSON (예: '{\"retrieval\":{\"query_rewrite\":{\"enable\":true}}}')"
    )
    parser.add_argument(
        "--override-b", default="",
        help="B 시나리오 config_override JSON"
    )
    parser.add_argument(
        "--backend-root",
        default=str(Path(__file__).resolve().parents[1] / "backend"),
        help="backend 패키지 sys.path 추가 (기본 ../backend)",
    )
    parser.add_argument("--output", help="결과 markdown 파일 저장 경로")
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
