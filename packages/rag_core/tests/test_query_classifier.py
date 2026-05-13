"""QueryClassifier — Tier 1 regex + Tier 2 LLM."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rag_core.clients.in_memory import InMemoryLLMClient
from rag_core.services.query_classifier import (
    ClassifierConfig,
    QueryClassifier,
)


REPO = Path(__file__).resolve().parents[3]


def _config_from_yaml() -> ClassifierConfig:
    """실제 platform/query_classifier.yaml을 로드해 dict로 변환 후 사용."""
    import yaml
    raw = yaml.safe_load(
        (REPO / "configs/platform/query_classifier.yaml").read_text(encoding="utf-8")
    )
    return ClassifierConfig.from_dict(raw)


def test_config_from_dict_with_defaults():
    c = ClassifierConfig.from_dict(None)
    assert c.tier2_enable is True
    assert c.tier2_fallback["query_type"] == "document_qa"


def test_config_loads_real_yaml():
    c = _config_from_yaml()
    assert c.tier2_enable is True
    rule_names = [r.get("name") for r in c.tier1_rules]
    assert "synthesis_pattern" in rule_names
    assert "inference_pattern" in rule_names


async def test_tier1_synthesis_pattern_matches():
    c = _config_from_yaml()
    clf = QueryClassifier()
    result = await clf.classify(question="A와 B의 차이를 비교해 주세요", config=c)
    assert result.tier1_matched == "synthesis_pattern"
    assert result.support_type == "synthesis"
    assert result.complexity == "medium"
    assert result.tier2_called is False


async def test_tier1_inference_pattern_matches():
    c = _config_from_yaml()
    clf = QueryClassifier()
    result = await clf.classify(
        question="이 경우 반출이 허용되는가?", config=c
    )
    assert result.tier1_matched == "inference_pattern"
    assert result.support_type == "inference"
    assert result.complexity == "high"


async def test_tier1_assessment_request():
    c = _config_from_yaml()
    clf = QueryClassifier()
    result = await clf.classify(question="시험 문제 만들어 줘", config=c)
    assert result.tier1_matched == "assessment_request"
    assert result.query_type == "assessment_generation"


async def test_tier1_greeting():
    c = _config_from_yaml()
    clf = QueryClassifier()
    result = await clf.classify(question="안녕하세요", config=c)
    assert result.tier1_matched == "greeting"
    assert result.query_type == "free_chat"


async def test_tier2_called_when_no_tier1_match():
    """Tier 1이 매치 안 되면 Tier 2 LLM 호출."""
    c = _config_from_yaml()
    response = json.dumps(
        {
            "query_type": "document_qa",
            "support_type": "direct",
            "complexity": "low",
        }
    )
    llm = InMemoryLLMClient(responses=[response])
    from rag_core.services.query_classifier import QueryClassifier
    prompt = QueryClassifier.load_tier2_prompt(
        REPO / "configs/platform/prompts/query_classifier_tier2.yaml"
    )
    clf = QueryClassifier(llm=llm, prompt=prompt, model="qwen-7b")
    result = await clf.classify(
        question="패스워드 정책의 구체적인 길이 기준은?", config=c
    )
    assert result.tier1_matched is None
    assert result.tier2_called is True
    assert result.tier2_parse_ok is True
    assert result.query_type == "document_qa"
    assert result.support_type == "direct"
    assert result.complexity == "low"


async def test_tier2_disabled_returns_fallback():
    c = ClassifierConfig.from_dict(
        {
            "tier1": [],
            "tier2": {
                "enable": False,
                "fallback_decision": {
                    "query_type": "document_qa",
                    "support_type": "direct",
                    "complexity": "medium",
                },
            },
        }
    )
    clf = QueryClassifier()
    result = await clf.classify(question="random", config=c)
    assert result.tier1_matched is None
    assert result.tier2_called is False
    assert result.tier2_parse_ok is False
    assert result.tier2_error == "tier2_disabled"
    assert result.query_type == "document_qa"


async def test_tier2_call_error_returns_fallback():
    """LLM 호출 자체 실패 시 fallback_decision 반영 + parse_ok=False."""
    c = _config_from_yaml()

    class FailingLLM:
        async def generate(self, *args, **kwargs):
            raise RuntimeError("vllm down")
        async def stream(self, *args, **kwargs):
            yield ""
        async def health(self):
            return False

    prompt = QueryClassifier.load_tier2_prompt(
        REPO / "configs/platform/prompts/query_classifier_tier2.yaml"
    )
    clf = QueryClassifier(llm=FailingLLM(), prompt=prompt, model="qwen-7b")
    result = await clf.classify(
        question="패스워드 정책의 길이 기준은?", config=c
    )
    assert result.tier1_matched is None
    assert result.tier2_called is True
    assert result.tier2_parse_ok is False
    assert "tier2_call_error" in (result.tier2_error or "")
    # fallback 적용됨
    assert result.query_type == "document_qa"


async def test_tier2_invalid_json_returns_fallback():
    c = _config_from_yaml()
    llm = InMemoryLLMClient(responses=["not a json"])
    prompt = QueryClassifier.load_tier2_prompt(
        REPO / "configs/platform/prompts/query_classifier_tier2.yaml"
    )
    clf = QueryClassifier(llm=llm, prompt=prompt, model="qwen-7b")
    result = await clf.classify(question="패스워드 정책 문의", config=c)
    assert result.tier2_parse_ok is False
    assert "json_decode_error" in (result.tier2_error or "")


def test_tier1_match_helper_skips_invalid_regex():
    rules = [
        {"name": "bad", "patterns": ["[unclosed"], "query_type": "x"},
        {"name": "good", "patterns": ["hello"], "query_type": "free_chat"},
    ]
    m = QueryClassifier.tier1_match("hello world", rules)
    assert m is not None and m["name"] == "good"
