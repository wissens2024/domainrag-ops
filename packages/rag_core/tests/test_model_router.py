"""ModelRouter — routing.yaml 룰 평가 + LoRA resolution."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from rag_core.services.model_router import ModelRouter
from rag_core.services.query_classifier import ClassificationResult


REPO = Path(__file__).resolve().parents[3]


def _platform_routing() -> dict:
    return yaml.safe_load(
        (REPO / "configs/platform/routing.yaml").read_text(encoding="utf-8")
    )


def test_default_route_when_no_rule_matches():
    cfg = {
        "default": {
            "model": "tenant_slm", "use_lora": True, "use_rag": True,
            "ui_mode": "chat_structured",
        },
        "rules": [],
    }
    cls = ClassificationResult(query_type="document_qa", complexity="low")
    d = ModelRouter.decide(classification=cls, routing_config=cfg)
    assert d.matched_rule == "default"
    assert d.model == "tenant_slm"
    assert d.use_lora is True


def test_synthesis_rule_matches_real_yaml():
    cfg = _platform_routing()
    cls = ClassificationResult(
        query_type="document_qa", support_type="synthesis", complexity="medium"
    )
    d = ModelRouter.decide(classification=cls, routing_config=cfg)
    assert d.matched_rule == "synthesis_answer"
    assert d.model == "tenant_slm"
    assert d.use_lora is True
    assert d.ui_mode == "chat_structured"


def test_inference_rule_picks_shared_llm_and_requires_judge():
    cfg = _platform_routing()
    cls = ClassificationResult(
        query_type="document_qa", support_type="inference", complexity="high"
    )
    d = ModelRouter.decide(classification=cls, routing_config=cfg)
    assert d.matched_rule == "inference_answer"
    assert d.model == "shared_llm"
    assert d.use_lora is False
    assert d.require_judge is True


def test_assessment_generation_rule():
    cfg = _platform_routing()
    cls = ClassificationResult(query_type="assessment_generation", complexity="high")
    d = ModelRouter.decide(classification=cls, routing_config=cfg)
    assert d.matched_rule == "assessment_generation"
    assert d.model == "shared_llm"
    assert d.require_similarity_check is True


def test_free_chat_falls_to_default_no_streaming_redirect():
    """ADR-023: free_chat_streaming 룰 폐기 — free_chat은 default로 흘러 검색을 켠다.

    streaming redirect/use_rag=false는 더 이상 발생하지 않는다. 근거 유무는 Gate 1이
    grounded/ungrounded로 분기한다.
    """
    cfg = _platform_routing()
    cls = ClassificationResult(query_type="free_chat", complexity="trivial")
    d = ModelRouter.decide(classification=cls, routing_config=cfg)
    assert d.matched_rule == "default"
    assert d.use_rag is True
    assert d.ui_mode == "chat_structured"
    assert d.action is None


def test_low_retrieval_confidence_no_routing_refusal():
    """ADR-023: low_retrieval_confidence→fallback_refusal 룰 폐기.

    낮은 retrieval_confidence는 라우팅 단계에서 거부되지 않고 default로 진행되며,
    근거 부족은 Gate 1(런타임)이 ungrounded 경로로 처리한다.
    """
    cfg = _platform_routing()
    cls = ClassificationResult(query_type="document_qa", complexity="medium")
    d = ModelRouter.decide(
        classification=cls, routing_config=cfg, retrieval_confidence=0.3
    )
    assert d.matched_rule == "default"
    assert d.action is None


def test_lora_resolution_from_tenant_model_config():
    cfg = {
        "default": {"model": "tenant_slm", "use_lora": True},
        "rules": [],
    }
    tenant_model = {
        "tenant_slm": {"endpoint": "vllm-tenant", "lora_adapter": "security-policy-v1"},
        "shared_llm": {"endpoint": "vllm-shared", "lora_adapter": None},
    }
    cls = ClassificationResult(query_type="document_qa", complexity="low")
    d = ModelRouter.decide(
        classification=cls, routing_config=cfg, tenant_model_config=tenant_model
    )
    assert d.lora_adapter == "security-policy-v1"


def test_lora_none_when_use_lora_false():
    cfg = {
        "default": {"model": "shared_llm", "use_lora": False},
        "rules": [],
    }
    tenant_model = {"shared_llm": {"lora_adapter": "should-not-be-used"}}
    cls = ClassificationResult(query_type="document_qa", complexity="low")
    d = ModelRouter.decide(
        classification=cls, routing_config=cfg, tenant_model_config=tenant_model
    )
    assert d.lora_adapter is None


def test_when_clause_supports_list():
    cfg = {
        "default": {"model": "tenant_slm"},
        "rules": [
            {
                "name": "complex_synthesis",
                "when": {"complexity": ["medium", "high"], "support_type": "synthesis"},
                "use_model": "shared_llm",
            }
        ],
    }
    cls = ClassificationResult(
        query_type="document_qa", support_type="synthesis", complexity="high"
    )
    d = ModelRouter.decide(classification=cls, routing_config=cfg)
    assert d.matched_rule == "complex_synthesis"
    assert d.model == "shared_llm"


def test_decision_to_log_dict():
    cfg = {"default": {"model": "tenant_slm"}, "rules": []}
    cls = ClassificationResult(query_type="document_qa")
    d = ModelRouter.decide(classification=cls, routing_config=cfg)
    log = d.to_log_dict()
    assert log["matched_rule"] == "default"
    assert log["model"] == "tenant_slm"
    assert "matched_signals" in log


def test_use_model_takes_precedence_over_model_in_rule():
    """rule level: use_model이 있으면 그것을, 없으면 model. (yaml inconsistency 흡수)."""
    cfg = {
        "default": {"model": "tenant_slm"},
        "rules": [
            {"name": "r1", "when": {}, "use_model": "shared_llm", "model": "ignored"},
        ],
    }
    cls = ClassificationResult(query_type="x")
    d = ModelRouter.decide(classification=cls, routing_config=cfg)
    assert d.model == "shared_llm"
