"""StreamingChatService unit tests — ADR-013 §6.2 (chat_streaming SSE)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from rag_core.clients.in_memory import InMemoryLLMClient
from rag_core.pii import RegexPIIDetector
from rag_core.services.chat_log_writer import InMemoryChatLogWriter
from rag_core.services.pii_service import PIIService
from rag_core.services.streaming_chat_service import (
    StreamingChatService,
    StreamingPrompt,
)

REPO = Path(__file__).resolve().parents[3]
PROMPT_YAML = REPO / "configs" / "platform" / "prompts" / "chat_streaming.yaml"
RULES_DIR = REPO / "packages" / "rag_core" / "rag_core" / "pii" / "rules"


def _pii_config() -> dict:
    return {
        "input": {
            "enable": True,
            "on_pii_found": {
                "high_severity": "block",
                "medium_severity": "warn",
                "low_severity": "log",
            },
        },
        "response": {"enable": True},
        "severity_map": {"rrn": "high", "phone": "medium", "email": "low"},
    }


def _build(
    *,
    stream_chunks: list[str] | None = None,
    raise_in_stream: bool = False,
) -> tuple[StreamingChatService, InMemoryLLMClient, InMemoryChatLogWriter]:
    chunks = stream_chunks if stream_chunks is not None else ["안녕", "하세요", "."]

    if raise_in_stream:
        class _BoomLLM(InMemoryLLMClient):
            async def stream(self, prompt, *, model, **kw):  # noqa: ARG002
                # 첫 토큰 한 개를 yield한 뒤 예외 — 부분 누적 검증
                yield "부분"
                raise RuntimeError("vllm down")

        llm = _BoomLLM(stream_chunks=chunks)
    else:
        llm = InMemoryLLMClient(stream_chunks=chunks)

    writer = InMemoryChatLogWriter()
    pii = PIIService(detector=RegexPIIDetector(rules_dir=RULES_DIR))
    svc = StreamingChatService(
        llm=llm,
        prompt=StreamingPrompt.load(PROMPT_YAML),
        default_model="qwen-7b",
        pii_service=pii,
        chat_log_writer=writer,
    )
    return svc, llm, writer


async def _collect(it: AsyncIterator) -> list:
    out = []
    async for evt in it:
        out.append(evt)
    return out


# --------------------------------------------------------------------------- #
# Token streaming
# --------------------------------------------------------------------------- #


async def test_stream_yields_tokens_then_complete() -> None:
    svc, llm, writer = _build(stream_chunks=["A", "B", "C"])
    events = await _collect(
        svc.stream(
            tenant_id="t1",
            user_id="u1",
            question="안녕",
            tenant_config={"pii": _pii_config()},
            conversation_id=None,
        )
    )
    assert [e.event for e in events] == ["token", "token", "token", "complete"]
    assert [e.data["text"] for e in events[:3]] == ["A", "B", "C"]
    final = events[-1]
    assert "message_id" in final.data
    assert final.data["metadata"]["ui_mode"] == "chat_streaming"
    assert final.data["metadata"]["citation_disabled"] is True

    # chat_logs 1건 — ui_mode=chat_streaming, citations=[], answer 누적
    assert len(writer.records) == 1
    log = writer.records[0]
    assert log.ui_mode == "chat_streaming"
    assert log.citations == []
    assert log.citation_types == []
    assert log.answer == "ABC"
    # tenant_slm 호출 1회
    stream_calls = [c for c in llm.calls if c["kind"] == "stream"]
    assert len(stream_calls) == 1


# --------------------------------------------------------------------------- #
# PII Layer 1 block
# --------------------------------------------------------------------------- #


async def test_input_pii_blocks_stream() -> None:
    svc, llm, writer = _build(stream_chunks=["never", "yielded"])
    events = await _collect(
        svc.stream(
            tenant_id="t1",
            user_id="u1",
            question="제 주민번호 901231-1234567",
            tenant_config={"pii": _pii_config()},
            conversation_id=None,
        )
    )
    # 단일 fallback event
    assert len(events) == 1
    assert events[0].event == "fallback"
    assert events[0].data["reason"] == "input_pii_blocked"
    assert "rrn" in events[0].data["blocked_categories"]
    # LLM stream은 호출되지 않음
    assert [c for c in llm.calls if c["kind"] == "stream"] == []
    # chat_logs는 input_pii_blocked로 기록
    assert len(writer.records) == 1
    log = writer.records[0]
    assert log.fallback_reason == "input_pii_blocked"
    assert log.ui_mode == "chat_streaming"
    assert any(f["category"] == "rrn" for f in log.input_pii_found)


# --------------------------------------------------------------------------- #
# PII Layer 4 — output mask (chat_logs only)
# --------------------------------------------------------------------------- #


async def test_output_pii_masked_in_chat_logs() -> None:
    """LLM이 RRN을 토큰 단위로 흘려도 chat_logs.answer는 마스킹되어 보관된다.
    클라이언트 send-time stream은 raw — multi-token PII 경계 보장 곤란(문서화된 한계).
    """
    svc, _, writer = _build(
        stream_chunks=["주민", "은 ", "901231-1234567", " 입니다"]
    )
    events = await _collect(
        svc.stream(
            tenant_id="t1",
            user_id="u1",
            question="안녕",
            tenant_config={"pii": _pii_config()},
            conversation_id=None,
        )
    )
    # 토큰들은 raw로 yield됨 (4 tokens + complete)
    assert events[-1].event == "complete"
    # chat_logs.answer는 마스킹된 form
    log = writer.records[0]
    assert "901231-1234567" not in log.answer
    assert any(f["category"] == "rrn" for f in log.output_pii_masked)
    # complete 이벤트 metadata에도 마스킹 메타 노출
    assert any(
        f["category"] == "rrn"
        for f in events[-1].data["metadata"]["pii"]["output_pii_masked"]
    )


# --------------------------------------------------------------------------- #
# LLM error fallback
# --------------------------------------------------------------------------- #


async def test_llm_error_emits_error_event_with_partial_log() -> None:
    svc, _, writer = _build(raise_in_stream=True)
    events = await _collect(
        svc.stream(
            tenant_id="t1",
            user_id="u1",
            question="안녕",
            tenant_config={"pii": _pii_config()},
            conversation_id=None,
        )
    )
    # 부분 토큰 1개 + error 이벤트
    assert events[0].event == "token"
    assert events[-1].event == "error"
    assert events[-1].data["reason"] == "llm_error"
    log = writer.records[0]
    assert log.fallback_reason and log.fallback_reason.startswith("llm_error:")
    # 부분 누적된 답변이 chat_logs에 보존
    assert "부분" in log.answer


# --------------------------------------------------------------------------- #
# No PII service / no chat_log_writer — graceful
# --------------------------------------------------------------------------- #


async def test_layer2_streaming_chat_log_masks_question_under_default() -> None:
    svc, _, writer = _build(stream_chunks=["답변", "입니다"])
    await _collect(
        svc.stream(
            tenant_id="t1",
            user_id="u1",
            question="문의는 user@example.com 으로 보내주세요",
            tenant_config={"pii": _pii_config()},
            conversation_id=None,
        )
    )
    log = writer.records[0]
    assert log.pii_storage_policy == "mask"
    assert "user@example.com" not in log.question


async def test_layer2_streaming_plain_keeps_raw_question_only_with_approval() -> None:
    """ADR-020 §4 — plain_approved=True 시 원문 보관 (platform_admin 승인 후)."""
    svc, _, writer = _build(stream_chunks=["x"])
    cfg = _pii_config()
    cfg["storage"] = {"pii_storage_policy": "plain", "plain_approved": True}
    await _collect(
        svc.stream(
            tenant_id="t1",
            user_id="u1",
            question="문의는 user@example.com 으로",
            tenant_config={"pii": cfg, "compliance_mode": "standard"},
            conversation_id=None,
        )
    )
    log = writer.records[0]
    assert log.pii_storage_policy == "plain"
    assert "user@example.com" in log.question


async def test_layer2_streaming_plain_without_approval_falls_back_to_mask() -> None:
    """plain_approved 누락 시 mask로 강제 fallback (정책 적용 일관성)."""
    svc, _, writer = _build(stream_chunks=["x"])
    cfg = _pii_config()
    cfg["storage"] = {"pii_storage_policy": "plain"}  # plain_approved 누락
    await _collect(
        svc.stream(
            tenant_id="t1",
            user_id="u1",
            question="문의는 user@example.com 으로",
            tenant_config={"pii": cfg, "compliance_mode": "standard"},
            conversation_id=None,
        )
    )
    log = writer.records[0]
    assert log.pii_storage_policy == "mask"
    assert "user@example.com" not in log.question


async def test_layer2_streaming_gdpr_strict_forces_mask() -> None:
    svc, _, writer = _build(stream_chunks=["x"])
    cfg = _pii_config()
    cfg["storage"] = {"pii_storage_policy": "plain"}
    await _collect(
        svc.stream(
            tenant_id="t1",
            user_id="u1",
            question="문의는 user@example.com 으로",
            tenant_config={"pii": cfg, "compliance_mode": "gdpr_strict"},
            conversation_id=None,
        )
    )
    log = writer.records[0]
    assert log.pii_storage_policy == "mask"
    assert "user@example.com" not in log.question


async def test_routing_decision_recorded_when_router_wired() -> None:
    """ADR-013 §1·§2 — model_router 주입 시 routing.yaml 룰이 평가되고 selected_model/lora가
    chat_logs.routing_decision + complete 이벤트 metadata에 채워진다."""
    from rag_core.services.model_router import ModelRouter

    chunks = ["A", "B"]
    llm = InMemoryLLMClient(stream_chunks=chunks)
    writer = InMemoryChatLogWriter()
    pii = PIIService(detector=RegexPIIDetector(rules_dir=RULES_DIR))
    router = ModelRouter()

    svc = StreamingChatService(
        llm=llm,
        prompt=StreamingPrompt.load(PROMPT_YAML),
        default_model="qwen-7b",
        pii_service=pii,
        chat_log_writer=writer,
        query_classifier=None,  # default classifier 없이도 router 동작 확인
        model_router=router,
    )
    tenant_cfg = {
        "pii": _pii_config(),
        "routing": {
            "default": {
                "model": "tenant_slm",
                "use_lora": True,
                "use_rag": False,
                "ui_mode": "chat_streaming",
            },
            "rules": [
                {
                    "name": "free_chat_default",
                    "when": {"query_type": "free_chat"},
                    "use_model": "tenant_slm",
                    "use_lora": True,
                    "use_rag": False,
                    "ui_mode": "chat_streaming",
                },
            ],
        },
        "model": {
            "tenant_slm": {"lora_adapter": "free-chat-v1"},
        },
    }

    events = await _collect(
        svc.stream(
            tenant_id="t1",
            user_id="u1",
            question="안녕하세요",
            tenant_config=tenant_cfg,
            conversation_id=None,
        )
    )
    final = events[-1]
    assert final.event == "complete"
    routing = final.data["metadata"]["routing_decision"]
    assert routing["matched_rule"] == "free_chat_default"
    assert routing["selected_model"] == "tenant_slm"
    assert routing["selected_lora"] == "free-chat-v1"
    assert routing["ui_mode"] == "chat_streaming"

    # chat_logs에도 동일하게 적재
    log = writer.records[0]
    assert log.routing_decision["matched_rule"] == "free_chat_default"
    assert log.routing_decision["selected_lora"] == "free-chat-v1"
    assert log.lora_adapter == "free-chat-v1"
    assert log.llm_model == "tenant_slm"


async def test_routing_fallback_refusal_short_circuits_stream() -> None:
    """routing.yaml의 action='fallback_refusal' 매치 시 LLM 호출 없이 fallback 이벤트로 종료."""
    from rag_core.services.model_router import ModelRouter

    llm = InMemoryLLMClient(stream_chunks=["should", "not", "stream"])
    writer = InMemoryChatLogWriter()
    pii = PIIService(detector=RegexPIIDetector(rules_dir=RULES_DIR))
    svc = StreamingChatService(
        llm=llm,
        prompt=StreamingPrompt.load(PROMPT_YAML),
        default_model="qwen-7b",
        pii_service=pii,
        chat_log_writer=writer,
        model_router=ModelRouter(),
    )
    tenant_cfg = {
        "pii": _pii_config(),
        "routing": {
            "default": {"model": "tenant_slm", "ui_mode": "chat_streaming"},
            "rules": [
                {
                    "name": "policy_refusal",
                    "when": {"query_type": "free_chat"},
                    "use_model": "tenant_slm",
                    "action": "fallback_refusal",
                    "ui_mode": "chat_streaming",
                },
            ],
        },
    }

    events = await _collect(
        svc.stream(
            tenant_id="t1",
            user_id="u1",
            question="질문",
            tenant_config=tenant_cfg,
            conversation_id=None,
        )
    )
    # token 0개 + 단일 fallback
    assert [e.event for e in events] == ["fallback"]
    assert events[0].data["reason"] == "routing_fallback_refusal"
    assert llm.calls == []  # LLM 미호출
    log = writer.records[0]
    assert log.fallback_reason == "routing_fallback_refusal"


async def test_works_without_pii_or_chat_log_writer() -> None:
    llm = InMemoryLLMClient(stream_chunks=["x", "y"])
    svc = StreamingChatService(
        llm=llm,
        prompt=StreamingPrompt.load(PROMPT_YAML),
        default_model="m",
        pii_service=None,
        chat_log_writer=None,
    )
    events = await _collect(
        svc.stream(
            tenant_id="t1",
            user_id="u1",
            question="q",
            tenant_config=None,
            conversation_id=None,
        )
    )
    assert [e.event for e in events] == ["token", "token", "complete"]
