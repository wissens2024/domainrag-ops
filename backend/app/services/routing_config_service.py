"""RoutingConfigService — ADR-017 §13 routing.yaml GET/PUT/dryrun orchestrator.

흐름:
  - GET: TenantConfigService.load(tid).routing (effective + 캐시)
  - PUT: validate schema → override_service.patch(cat='routing', key='', value=full_dict)
         + TenantConfigService.apply_runtime_override(tid, 'routing', value) — 즉시 반영
  - dryrun: ModelRouter.decide(classifier_decision, routing_config, model_config)

PUT 후 효과: TenantConfigService.invalidate(tid)로 다음 load()부터 적용. runtime
override layer로 같은 프로세스 내 모든 호출이 새 routing을 사용한다(InMemory 테스트 +
운영 multi-instance는 ADR-009 §5 LISTEN/NOTIFY로 동기화 — 별도 작업).

schema 검증:
  - top-level: dict, schema_version int (optional)
  - rules: list[dict] (optional). 각 rule의 when은 dict, action/use_model/use_lora 등
  - 알 수 없는 출력 키는 통과(yaml 확장 여지). when 키만 화이트리스트 검증.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rag_core.services.model_router import ModelRouter, RoutingDecision
from rag_core.services.query_classifier import ClassificationResult


_ALLOWED_WHEN_KEYS = {
    "query_type",
    "support_type",
    "complexity",
    "retrieval_confidence_below",
}


class RoutingSchemaError(ValueError):
    """routing.yaml schema 검증 실패."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class DryRunRequest:
    sample_query: str | None
    classifier_decision: dict[str, Any]
    routing_config: dict[str, Any] | None  # None이면 현재 effective 사용
    retrieval_confidence: float | None = None


@dataclass
class DryRunResult:
    matched_rule: str
    model: str
    use_lora: bool
    use_rag: bool
    ui_mode: str
    require_judge: bool
    require_similarity_check: bool
    action: str | None
    lora_adapter: str | None
    matched_signals: dict[str, Any]


def validate_routing_yaml(value: Any) -> None:
    """ADR-013 §1 + ADR-017 §13 — yaml schema 가벼운 lint.

    raises:
        RoutingSchemaError: 위반 사항 목록
    """
    errors: list[str] = []
    if not isinstance(value, dict):
        raise RoutingSchemaError(["routing_yaml_not_dict"])
    rules = value.get("rules")
    if rules is not None and not isinstance(rules, list):
        errors.append("rules_not_list")
    default = value.get("default")
    if default is not None and not isinstance(default, dict):
        errors.append("default_not_dict")

    if isinstance(rules, list):
        for i, rule in enumerate(rules):
            if not isinstance(rule, dict):
                errors.append(f"rules[{i}]_not_dict")
                continue
            when = rule.get("when")
            if when is not None and not isinstance(when, dict):
                errors.append(f"rules[{i}].when_not_dict")
            elif isinstance(when, dict):
                bad = [k for k in when if k not in _ALLOWED_WHEN_KEYS]
                if bad:
                    errors.append(
                        f"rules[{i}].when_unknown_keys={bad}"
                    )
            # use_model 또는 model 또는 action 중 하나는 있어야 한다
            if not (
                rule.get("use_model")
                or rule.get("model")
                or rule.get("action")
            ):
                errors.append(f"rules[{i}]_missing_use_model_or_action")
    if errors:
        raise RoutingSchemaError(errors)


def dryrun_decide(
    *,
    routing_config: dict[str, Any],
    classifier_decision: dict[str, Any],
    tenant_model_config: dict[str, Any] | None = None,
    retrieval_confidence: float | None = None,
) -> RoutingDecision:
    """ModelRouter.decide() 한 번 호출. 외부에서는 dict로 받지만 내부에서는
    ClassificationResult로 변환."""
    classification = ClassificationResult(
        query_type=str(classifier_decision.get("query_type") or "document_qa"),
        support_type=classifier_decision.get("support_type"),
        complexity=classifier_decision.get("complexity"),
    )
    return ModelRouter.decide(
        classification=classification,
        routing_config=routing_config,
        tenant_model_config=tenant_model_config,
        retrieval_confidence=retrieval_confidence,
    )


def routing_decision_to_dict(d: RoutingDecision) -> dict[str, Any]:
    return {
        "matched_rule": d.matched_rule,
        "model": d.model,
        "use_lora": d.use_lora,
        "use_rag": d.use_rag,
        "ui_mode": d.ui_mode,
        "require_judge": d.require_judge,
        "require_similarity_check": d.require_similarity_check,
        "action": d.action,
        "lora_adapter": d.lora_adapter,
        "matched_signals": d.matched_signals,
    }
