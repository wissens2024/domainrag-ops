"""ModelRouter — ADR-013 §1·§2 yaml 룰 평가.

routing.yaml 의 rules 배열을 위에서 아래로 평가, 첫 매치 적용. 매치 없으면 default.
ClassificationResult의 query_type/support_type/complexity와 retrieval_confidence 등
optional signal을 룰의 `when` 절에 매칭한다.

지원 when 키:
  - query_type, support_type, complexity (단일 문자열 또는 list)
  - retrieval_confidence_below: float (선택, retrieval 후 평가 시 사용)

지원 출력 키 (rule):
  - use_model | model
  - use_lora (bool)
  - use_rag (bool)
  - ui_mode
  - require_judge (bool)
  - require_similarity_check (bool)
  - action ("fallback_refusal" 등)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .query_classifier import ClassificationResult


@dataclass
class RoutingDecision:
    matched_rule: str
    model: str
    use_lora: bool = True
    use_rag: bool = True
    ui_mode: str = "chat_structured"
    require_judge: bool = False
    require_similarity_check: bool = False
    action: str | None = None
    # tenant model.yaml에서 해석된 LoRA adapter (use_lora=True일 때만)
    lora_adapter: str | None = None
    matched_signals: dict[str, Any] = field(default_factory=dict)

    def to_log_dict(self) -> dict[str, Any]:
        return asdict(self)


def _matches(when: dict | None, signals: dict[str, Any]) -> bool:
    """룰의 when 절을 signals와 매칭. 모든 명시 조건 AND 매칭되어야 True."""
    if not when:
        return True
    for key, expected in when.items():
        if key == "retrieval_confidence_below":
            actual = signals.get("retrieval_confidence")
            if actual is None or actual >= float(expected):
                return False
            continue
        actual = signals.get(key)
        if isinstance(expected, list):
            if actual not in expected:
                return False
        else:
            if actual != expected:
                return False
    return True


def _resolve_lora(
    *,
    use_lora: bool,
    model_key: str,
    tenant_model_config: dict[str, Any],
) -> str | None:
    """tenant `model.yaml`에서 endpoint_key의 lora_adapter 추출.

    tenant_model_config 예시:
      {
        "tenant_slm": {"endpoint": "...", "lora_adapter": "security-policy-v1"},
        "shared_llm": {"endpoint": "...", "lora_adapter": null},
      }
    """
    if not use_lora:
        return None
    endpoint_cfg = (tenant_model_config or {}).get(model_key) or {}
    lora = endpoint_cfg.get("lora_adapter")
    return lora if lora else None


class ModelRouter:
    """routing.yaml + tenant model.yaml을 받아 RoutingDecision 산출."""

    @staticmethod
    def decide(
        *,
        classification: ClassificationResult,
        routing_config: dict[str, Any] | None,
        tenant_model_config: dict[str, Any] | None = None,
        retrieval_confidence: float | None = None,
    ) -> RoutingDecision:
        cfg = routing_config or {}
        rules = cfg.get("rules") or []
        default = cfg.get("default") or {}

        signals: dict[str, Any] = {
            "query_type": classification.query_type,
            "support_type": classification.support_type,
            "complexity": classification.complexity,
        }
        if retrieval_confidence is not None:
            signals["retrieval_confidence"] = retrieval_confidence

        for rule in rules:
            when = rule.get("when") or {}
            if _matches(when, signals):
                model_key = str(rule.get("use_model") or rule.get("model") or default.get("model") or "tenant_slm")
                use_lora = bool(rule.get("use_lora", default.get("use_lora", True)))
                lora = _resolve_lora(
                    use_lora=use_lora,
                    model_key=model_key,
                    tenant_model_config=tenant_model_config or {},
                )
                return RoutingDecision(
                    matched_rule=str(rule.get("name") or "unnamed"),
                    model=model_key,
                    use_lora=use_lora,
                    use_rag=bool(rule.get("use_rag", default.get("use_rag", True))),
                    ui_mode=str(rule.get("ui_mode", default.get("ui_mode", "chat_structured"))),
                    require_judge=bool(rule.get("require_judge", False)),
                    require_similarity_check=bool(rule.get("require_similarity_check", False)),
                    action=rule.get("action"),
                    lora_adapter=lora,
                    matched_signals=dict(signals),
                )

        # default
        model_key = str(default.get("model") or "tenant_slm")
        use_lora = bool(default.get("use_lora", True))
        lora = _resolve_lora(
            use_lora=use_lora,
            model_key=model_key,
            tenant_model_config=tenant_model_config or {},
        )
        return RoutingDecision(
            matched_rule="default",
            model=model_key,
            use_lora=use_lora,
            use_rag=bool(default.get("use_rag", True)),
            ui_mode=str(default.get("ui_mode", "chat_structured")),
            lora_adapter=lora,
            matched_signals=dict(signals),
        )
