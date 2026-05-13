"""ChatLogPayload + InMemoryChatLogWriter — 계약·자동 conversation_id."""

from __future__ import annotations

from rag_core.services.chat_log_writer import (
    ChatLogPayload,
    InMemoryChatLogWriter,
)


def _payload(**kwargs) -> ChatLogPayload:
    base = dict(
        tenant_id="security",
        request_id="r1",
        user_id="u1",
        conversation_id=None,
        question="q",
        answer="a",
    )
    base.update(kwargs)
    return ChatLogPayload(**base)


async def test_writer_appends_record():
    w = InMemoryChatLogWriter()
    cid = await w.write(_payload())
    assert cid.startswith("conv-")
    assert len(w.records) == 1
    rec = w.records[0]
    assert rec.tenant_id == "security"
    assert rec.conversation_id == cid


async def test_writer_uses_supplied_conversation_id():
    w = InMemoryChatLogWriter()
    cid = await w.write(_payload(conversation_id="existing-conv-001"))
    assert cid == "existing-conv-001"
    assert w.records[0].conversation_id == "existing-conv-001"


async def test_writer_preserves_excerpts():
    """audit truth — citation excerpt가 그대로 저장된다."""
    w = InMemoryChatLogWriter()
    citations = [
        {"chunk_id": "c1", "marker": "[1]", "claim_text": "X",
         "excerpt": "원문 발췌", "support_level": "strong", "verified": True},
    ]
    retrieved = [
        {"chunk_id": "c1", "title": "T", "content": "원문 그대로", "page_number": 1},
    ]
    await w.write(_payload(citations=citations, retrieved_chunks=retrieved))
    rec = w.records[0]
    assert rec.citations[0]["excerpt"] == "원문 발췌"
    assert rec.retrieved_chunks[0]["content"] == "원문 그대로"


async def test_writer_records_all_columns():
    w = InMemoryChatLogWriter()
    await w.write(_payload(
        citation_types=["direct", "synthesis"],
        ui_mode="chat_structured",
        confidence=0.82,
        fallback_reason=None,
        unsupported_ratio=0.0,
        verifier_metrics={"tier1_markers_removed": 0, "tier2_avg_similarity": 0.81},
        routing_decision={"matched_rule": "synthesis_answer"},
        classifier_decision={"query_type": "document_qa"},
        model_failure_chain=[{"model": "tenant_slm", "failure": "timeout"}],
        inference_judge_results=[{"valid": True, "confidence": 0.78}],
        latency_ms=3120,
        llm_model="qwen-7b",
        lora_adapter="security-v1",
    ))
    r = w.records[0]
    assert r.citation_types == ["direct", "synthesis"]
    assert r.confidence == 0.82
    assert r.verifier_metrics["tier2_avg_similarity"] == 0.81
    assert r.routing_decision["matched_rule"] == "synthesis_answer"
    assert r.classifier_decision["query_type"] == "document_qa"
    assert r.model_failure_chain[0]["failure"] == "timeout"
    assert r.inference_judge_results[0]["valid"] is True
    assert r.lora_adapter == "security-v1"
    assert r.latency_ms == 3120


async def test_writer_records_fallback_path():
    """gate fail 응답도 저장된다 (CLAUDE.md 원칙 9)."""
    w = InMemoryChatLogWriter()
    await w.write(_payload(
        answer="확인 불가 fallback",
        fallback_reason="gate2_below_min_verified_count",
        confidence=0.0,
        citations=[],
        citation_types=[],
    ))
    r = w.records[0]
    assert r.fallback_reason == "gate2_below_min_verified_count"
    assert r.citations == []


async def test_writer_distinct_records_for_multiple_calls():
    w = InMemoryChatLogWriter()
    cid1 = await w.write(_payload(request_id="r1"))
    cid2 = await w.write(_payload(request_id="r2"))
    assert cid1 != cid2
    assert len(w.records) == 2
