"""ADR-013 §7 fallback chain 양성 경로 + 중간 단계 회복 검증.

generate_answer_node가 primary → tenant_slm_no_lora → shared_llm 순서로 시도하고,
각 단계별 model_failure_chain 누적 후 첫 성공에서 종료하는지 검증.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from rag_core.clients.in_memory import InMemoryLLMClient
from rag_core.interfaces.retriever import RetrievedChunk
from rag_core.services.generation_service import (
    GenerationPrompt,
    GenerationService,
)
from rag_core.workflows.nodes import RAGGraphDeps, generate_answer_node
from rag_core.workflows.rag_graph import RAGState


_VALID_JSON = json.dumps(
    {"answer_segments": [{"text": "answer", "citations": [1]}], "limitations": None}
)


def _prompt() -> GenerationPrompt:
    return GenerationPrompt(
        system="system",
        user="질문: {{ question }}\n{% for c in contexts %}[{{ loop.index }}] {{ c.title }}\n{% endfor %}",
        response_schema={"type": "object"},
    )


def _state(model: str = "tenant_slm", lora: str | None = "security-v1") -> RAGState:
    state = RAGState(
        request_id="r1",
        tenant_id="security",
        user_id="u1",
        question="패스워드?",
    )
    state.selected_model = model
    state.selected_lora = lora
    state.final_contexts = [
        {
            "chunk_id": "c1",
            "doc_id": "d1",
            "title": "정책",
            "content": "패스워드는 12자 이상",
            "fused_score": 0.9,
            "rerank_score": 0.8,
        }
    ]
    return state


def _deps(llm: InMemoryLLMClient) -> RAGGraphDeps:
    gen = GenerationService(llm=llm, prompt=_prompt(), model="tenant_slm")
    # generate_answer_node는 deps.generation_service만 사용
    return RAGGraphDeps(
        retrieval_service=None,  # type: ignore[arg-type]
        generation_service=gen,
        config_loader=lambda _tid: {},
    )


@pytest.mark.asyncio
async def test_primary_success_no_fallback():
    """첫 호출이 valid JSON이면 단일 호출에서 종료, model_failure_chain 빈 상태."""
    llm = InMemoryLLMClient(responses=[_VALID_JSON])
    state = _state()
    out = await generate_answer_node(state, _deps(llm))

    assert out["selected_model"] == "tenant_slm"
    assert out["selected_lora"] == "security-v1"
    assert out["model_failure_chain"] == []
    assert out["answer_segments"]
    assert "fallback_reason" not in out
    # LLM 1회만 호출
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_lora_step_failure_recovers_with_no_lora():
    """primary(LoRA) 실패 → tenant_slm_no_lora가 성공."""
    llm = InMemoryLLMClient(responses=["not json", _VALID_JSON])
    state = _state()
    out = await generate_answer_node(state, _deps(llm))

    assert out["selected_model"] == "tenant_slm"
    assert out["selected_lora"] is None  # no_lora 단계
    assert len(out["model_failure_chain"]) == 1
    assert out["model_failure_chain"][0]["label"] == "primary"
    assert out["model_failure_chain"][0]["lora"] == "security-v1"
    assert out["model_failure_chain"][0]["reason"] == "parse_error"
    assert "fallback_reason" not in out


@pytest.mark.asyncio
async def test_two_steps_fail_shared_llm_recovers():
    """primary + no_lora 둘 다 실패 → shared_llm 성공."""
    llm = InMemoryLLMClient(responses=["bad1", "bad2", _VALID_JSON])
    state = _state()
    out = await generate_answer_node(state, _deps(llm))

    assert out["selected_model"] == "shared_llm"
    assert out["selected_lora"] is None
    assert len(out["model_failure_chain"]) == 2
    labels = [e["label"] for e in out["model_failure_chain"]]
    assert labels == ["primary", "tenant_slm_no_lora"]


@pytest.mark.asyncio
async def test_all_steps_fail_returns_fallback():
    """3 단계 모두 실패 → fallback_reason='low_generation_quality'."""
    llm = InMemoryLLMClient(responses=["x", "y", "z"])
    state = _state()
    out = await generate_answer_node(state, _deps(llm))

    assert out["fallback_reason"] == "low_generation_quality"
    assert out["error"] == "all_models_failed"
    assert len(out["model_failure_chain"]) == 3
    assert out["answer_segments"] == []


@pytest.mark.asyncio
async def test_primary_shared_llm_skips_no_lora_step():
    """router가 처음부터 shared_llm 선택했으면 no_lora 단계는 건너뜀 (lora 자체 없음).

    chain steps = [shared_llm-primary] 단일 — primary 실패 시 chain 끝.
    """
    llm = InMemoryLLMClient(responses=["bad"])
    state = _state(model="shared_llm", lora=None)
    out = await generate_answer_node(state, _deps(llm))

    assert out["fallback_reason"] == "low_generation_quality"
    # primary shared_llm만 시도 (no_lora 단계 없음, shared_llm fallback도 자기 자신이라 추가 안 됨)
    assert len(out["model_failure_chain"]) == 1
    assert out["model_failure_chain"][0]["model"] == "shared_llm"


@pytest.mark.asyncio
async def test_no_lora_chain_skips_no_lora_step():
    """router가 tenant_slm + lora=None 선택했으면 no_lora 단계 skip (이미 no_lora 상태)."""
    llm = InMemoryLLMClient(responses=["bad", _VALID_JSON])
    state = _state(model="tenant_slm", lora=None)
    out = await generate_answer_node(state, _deps(llm))

    # primary(no_lora) 실패 → shared_llm 성공
    assert out["selected_model"] == "shared_llm"
    assert len(out["model_failure_chain"]) == 1
    assert out["model_failure_chain"][0]["label"] == "primary"
    assert out["model_failure_chain"][0]["lora"] is None
