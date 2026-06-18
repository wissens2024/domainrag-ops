"""JudgeService — Jinja2 prompt + guided_json + JSON 파싱 + min_confidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rag_core.clients.in_memory import InMemoryLLMClient
from rag_core.interfaces.retriever import RetrievedChunk
from rag_core.services.judge_service import JudgePrompt, JudgeService


REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def real_prompt() -> JudgePrompt:
    return JudgePrompt.load(
        prompt_yaml=REPO / "configs/platform/prompts/inference_judge.yaml",
        schema_json=REPO / "configs/platform/prompts/inference_judge_schema.json",
    )


def _chunk(cid: str, content: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=cid, doc_id="d", title="t", content=content,
        page_number=1, section_title=None,
        dense_score=0.0, sparse_score=0.0, fused_score=0.0, payload={},
    )


def test_load_real_prompt(real_prompt):
    assert real_prompt.system
    assert "{{ claim_text }}" in real_prompt.user
    assert "valid" in real_prompt.response_schema["properties"]
    assert real_prompt.min_confidence == 0.6


async def test_judge_passes_high_confidence(real_prompt):
    response = json.dumps(
        {
            "valid": True,
            "confidence": 0.85,
            "reasoning": "두 청크의 내용 결합으로 도출 가능",
            "caveat": None,
        },
        ensure_ascii=False,
    )
    llm = InMemoryLLMClient(responses=[response])
    svc = JudgeService(llm=llm, prompt=real_prompt, model="qwen2.5-7b-awq")

    result = await svc.judge(
        claim_text="이로부터 보안성이 매우 높다고 추론됩니다",
        cited_chunks=[_chunk("c1", "패스워드 12자 이상 + 만료 90일")],
    )
    assert result.valid is True
    assert result.confidence == 0.85
    assert result.passes(min_confidence=0.6) is True
    assert result.parse_ok is True


async def test_judge_fails_low_confidence(real_prompt):
    response = json.dumps(
        {
            "valid": True,
            "confidence": 0.5,
            "reasoning": "근거 부분적으로만 지지",
            "caveat": "외부 지식 일부 사용 의심",
        }
    )
    llm = InMemoryLLMClient(responses=[response])
    svc = JudgeService(llm=llm, prompt=real_prompt, model="qwen2.5-7b-awq")

    result = await svc.judge(
        claim_text="claim",
        cited_chunks=[_chunk("c1", "x")],
    )
    assert result.valid is True
    assert result.confidence == 0.5
    assert result.passes(min_confidence=0.6) is False  # below threshold


async def test_judge_invalid_decision(real_prompt):
    response = json.dumps(
        {"valid": False, "confidence": 0.2, "reasoning": "근거 부재"}
    )
    llm = InMemoryLLMClient(responses=[response])
    svc = JudgeService(llm=llm, prompt=real_prompt, model="qwen2.5-7b-awq")
    result = await svc.judge(claim_text="x", cited_chunks=[_chunk("c1", "y")])
    assert result.valid is False
    assert result.passes(min_confidence=0.6) is False


async def test_judge_handles_invalid_json(real_prompt):
    llm = InMemoryLLMClient(responses=["not a json"])
    svc = JudgeService(llm=llm, prompt=real_prompt, model="qwen2.5-7b-awq")
    result = await svc.judge(claim_text="x", cited_chunks=[_chunk("c1", "y")])
    assert result.parse_ok is False
    assert result.valid is False
    assert result.parse_error is not None


async def test_judge_handles_schema_violation(real_prompt):
    # confidence 누락
    llm = InMemoryLLMClient(responses=['{"valid": true, "reasoning": "x"}'])
    svc = JudgeService(llm=llm, prompt=real_prompt, model="qwen2.5-7b-awq")
    result = await svc.judge(claim_text="x", cited_chunks=[_chunk("c1", "y")])
    assert result.parse_ok is False
    assert result.valid is False
    assert "schema_violation" in (result.parse_error or "")


async def test_judge_passes_lora_independent(real_prompt):
    """judge는 LoRA를 사용하지 않는다 (shared_llm은 multi_lora=false)."""
    response = json.dumps({"valid": True, "confidence": 0.8, "reasoning": "ok"})
    llm = InMemoryLLMClient(responses=[response])
    svc = JudgeService(llm=llm, prompt=real_prompt, model="qwen2.5-7b-awq")
    await svc.judge(claim_text="x", cited_chunks=[_chunk("c1", "y")])
    assert llm.calls[0]["lora_adapter"] is None


async def test_judge_renders_prompt_with_claim_and_chunks(real_prompt):
    response = json.dumps({"valid": True, "confidence": 0.7, "reasoning": "ok"})
    llm = InMemoryLLMClient(responses=[response])
    svc = JudgeService(llm=llm, prompt=real_prompt, model="qwen2.5-7b-awq")
    await svc.judge(
        claim_text="추론 결론",
        cited_chunks=[_chunk("c1", "근거1"), _chunk("c2", "근거2")],
        inference_chain="A→B→C",
    )
    rendered = llm.calls[0]["prompt"]
    assert "추론 결론" in rendered
    assert "근거1" in rendered and "근거2" in rendered
    assert "[1]" in rendered and "[2]" in rendered
    # guided_json schema 전달
    assert "valid" in llm.calls[0]["guided_json_schema"]["properties"]
