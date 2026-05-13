"""GenerationService — Jinja2 prompt 렌더 + LLM guided_json + JSON 파싱."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rag_core.clients.in_memory import InMemoryLLMClient
from rag_core.interfaces.retriever import RetrievedChunk
from rag_core.services.generation_service import (
    GenerationPrompt,
    GenerationService,
)


@pytest.fixture
def simple_prompt() -> GenerationPrompt:
    return GenerationPrompt(
        system="당신은 챗봇입니다.",
        user="질문: {{ question }}\n\nContext:\n"
        "{% for c in contexts %}[{{ loop.index }}] {{ c.title }}: {{ c.content }}\n"
        "{% endfor %}",
        response_schema={
            "type": "object",
            "required": ["answer_segments"],
            "properties": {
                "answer_segments": {"type": "array"},
                "limitations": {"type": ["string", "null"]},
            },
        },
    )


def _chunk(cid: str, title: str, content: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=cid,
        doc_id="d1",
        title=title,
        content=content,
        page_number=1,
        section_title="S",
        dense_score=0.0,
        sparse_score=0.0,
        fused_score=0.0,
        payload={},
    )


async def test_generate_structured_parses_valid_json(simple_prompt):
    response_obj = {
        "answer_segments": [
            {"text": "패스워드는 12자 이상", "citations": [1], "support_type": "direct"}
        ],
        "limitations": None,
    }
    llm = InMemoryLLMClient(responses=[json.dumps(response_obj, ensure_ascii=False)])
    svc = GenerationService(llm=llm, prompt=simple_prompt, model="qwen-7b")

    result = await svc.generate_structured(
        question="패스워드 길이?",
        contexts=[_chunk("c1", "Password Policy", "12자 이상")],
    )
    assert result.parse_ok is True
    assert result.parse_error is None
    assert len(result.answer_segments) == 1
    assert result.answer_segments[0]["citations"] == [1]
    assert result.limitations is None
    # LLM에 guided_json_schema가 전달됐는지
    call = llm.calls[0]
    assert call["guided_json_schema"] is not None
    assert "answer_segments" in call["guided_json_schema"]["properties"]


async def test_generate_structured_invalid_json(simple_prompt):
    llm = InMemoryLLMClient(responses=["not a json"])
    svc = GenerationService(llm=llm, prompt=simple_prompt, model="qwen-7b")
    result = await svc.generate_structured(question="q", contexts=[])
    assert result.parse_ok is False
    assert result.parse_error is not None
    assert result.answer_segments == []
    assert result.raw_response == "not a json"


async def test_generate_structured_segments_not_list(simple_prompt):
    llm = InMemoryLLMClient(responses=['{"answer_segments": "not-a-list"}'])
    svc = GenerationService(llm=llm, prompt=simple_prompt, model="qwen-7b")
    result = await svc.generate_structured(question="q", contexts=[])
    assert result.parse_ok is False
    assert "not a list" in result.parse_error


async def test_render_substitutes_question_and_contexts(simple_prompt):
    llm = InMemoryLLMClient(
        responses=[json.dumps({"answer_segments": [{"text": "x", "citations": []}]})]
    )
    svc = GenerationService(llm=llm, prompt=simple_prompt, model="qwen-7b")
    await svc.generate_structured(
        question="q1",
        contexts=[
            _chunk("c1", "DocA", "내용A"),
            _chunk("c2", "DocB", "내용B"),
        ],
        lora_adapter="security-v1",
    )
    rendered = llm.calls[0]["prompt"]
    assert "q1" in rendered
    assert "DocA" in rendered and "내용A" in rendered
    assert "[1]" in rendered and "[2]" in rendered
    assert llm.calls[0]["lora_adapter"] == "security-v1"


def test_load_from_files(tmp_path: Path):
    prompt_yaml = tmp_path / "rag.yaml"
    prompt_yaml.write_text(
        "system: 시스템\nuser: '질문: {{ question }}'\n", encoding="utf-8"
    )
    schema_json = tmp_path / "schema.json"
    schema_json.write_text(
        json.dumps({"type": "object", "required": ["answer_segments"]}),
        encoding="utf-8",
    )
    p = GenerationPrompt.load(prompt_yaml=prompt_yaml, schema_json=schema_json)
    assert p.system == "시스템"
    assert "{{ question }}" in p.user
    assert p.response_schema["required"] == ["answer_segments"]


def test_load_actual_rag_answer_yaml():
    """실제 configs/platform/prompts/rag_answer.yaml + answer_schema.json이 로드 가능."""
    repo = Path(__file__).resolve().parents[3]
    p = GenerationPrompt.load(
        prompt_yaml=repo / "configs/platform/prompts/rag_answer.yaml",
        schema_json=repo / "configs/platform/prompts/answer_schema.json",
    )
    assert p.system  # 비어있지 않음
    assert "{{ question }}" in p.user
    assert "answer_segments" in p.response_schema["properties"]
