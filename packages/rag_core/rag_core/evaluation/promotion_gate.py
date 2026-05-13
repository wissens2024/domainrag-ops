"""PromotionGate — ADR-009 §7. promotion_gate.yaml로 EvaluationSummary 합/불 판정.

yaml 예시:
    metrics:
      retrieval_recall_at_5: { min: 0.85 }
      citation_accuracy: { min: 0.80 }
      unsupported_ratio: { max: 0.20 }
      fallback_rate: { max: 0.15 }
    auto_promote: false
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from rag_core.evaluation.metrics import EvaluationSummary


@dataclass
class GateMetricResult:
    name: str
    actual: float
    bound: float
    bound_kind: str  # 'min' | 'max'
    passed: bool


@dataclass
class GateResult:
    passed: bool
    metrics: list[GateMetricResult] = field(default_factory=list)
    auto_promote: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "auto_promote": self.auto_promote,
            "metrics": [
                {
                    "name": m.name,
                    "actual": m.actual,
                    "bound": m.bound,
                    "bound_kind": m.bound_kind,
                    "passed": m.passed,
                }
                for m in self.metrics
            ],
        }


class PromotionGate:
    """단일 promotion_gate 정의를 캡슐화.

    evaluate(summary) — EvaluationSummary 비교. 모든 metric이 통과해야 전체 passed.
    """

    def __init__(self, *, metrics: dict[str, dict[str, float]], auto_promote: bool = False):
        self.metrics_cfg = metrics
        self.auto_promote = auto_promote

    def evaluate(self, summary: EvaluationSummary) -> GateResult:
        summary_dict = summary.to_dict()
        results: list[GateMetricResult] = []
        all_pass = True
        for name, bounds in self.metrics_cfg.items():
            actual = float(summary_dict.get(name, 0.0))
            if "min" in bounds:
                bound = float(bounds["min"])
                passed = actual >= bound
                results.append(
                    GateMetricResult(
                        name=name, actual=actual, bound=bound,
                        bound_kind="min", passed=passed,
                    )
                )
            elif "max" in bounds:
                bound = float(bounds["max"])
                passed = actual <= bound
                results.append(
                    GateMetricResult(
                        name=name, actual=actual, bound=bound,
                        bound_kind="max", passed=passed,
                    )
                )
            else:
                # bound 누락 — 기본 통과로 두되 별도 metric 등록
                results.append(
                    GateMetricResult(
                        name=name, actual=actual, bound=0.0,
                        bound_kind="none", passed=True,
                    )
                )
            if not results[-1].passed:
                all_pass = False
        return GateResult(passed=all_pass, metrics=results, auto_promote=self.auto_promote)


def load_gate_yaml(path: Path) -> PromotionGate:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return PromotionGate(
        metrics=raw.get("metrics") or {},
        auto_promote=bool(raw.get("auto_promote", False)),
    )
