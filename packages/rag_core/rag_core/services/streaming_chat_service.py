"""StreamingChatService — ADR-013 §6.2 chat_streaming SSE 모드.

본 모드는 RAG 미사용 + citation 비활성(`citation_disabled: true`)인 자유 대화 모드.
LangGraph는 사용하지 않고 직접 LLM stream을 SSE 토큰 이벤트로 forward.

흐름:
  1. PII Layer 1 (ADR-020 §3) — high severity → block, fallback 이벤트 emit, save_chat_log
  2. LLM stream 시작 — 각 token을 'token' 이벤트로 yield
  3. 스트림 완료 후 full text PII Layer 4 마스킹 → chat_logs.answer 보관 (audit truth)
  4. 'complete' 이벤트로 conversation_id/latency/PII 메타 emit
  5. save_chat_log 호출 — ui_mode='chat_streaming', citations=[], citation_types=[]

Layer 4 마스킹은 토큰 단위 실시간 적용이 아닌 chat_logs 기록 시점에 적용한다 (PII는
multi-token 패턴이므로 실시간 마스킹은 분리 보장이 어렵다 — ADR-020 §6 보강 사항).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Template

from ..interfaces.llm_client import LLMClient
from .chat_log_writer import ChatLogPayload, ChatLogWriter
from .model_router import ModelRouter
from .pii_service import PIIService
from .query_classifier import (
    ClassificationResult,
    ClassifierConfig,
    QueryClassifier,
)


@dataclass
class StreamEvent:
    """SSE event 1건. data는 dict (백엔드가 JSON 직렬화)."""

    event: str  # token | complete | error | fallback
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamingPrompt:
    """configs/platform/prompts/chat_streaming.yaml 매핑."""

    system: str
    user: Template

    @classmethod
    def load(cls, prompt_yaml: Path) -> "StreamingPrompt":
        with prompt_yaml.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(
            system=str(data.get("system", "")),
            user=Template(str(data.get("user", "{{ question }}"))),
        )

    def render(self, question: str) -> str:
        return f"{self.system}\n\n{self.user.render(question=question)}".strip()


class StreamingChatService:
    """ADR-013 §6.2 chat_streaming 처리기.

    Args:
        llm: tenant_slm 기본. routing이 shared_llm 결정 시 caller가 다른 인스턴스 주입.
        prompt: chat_streaming.yaml 프롬프트.
        default_model: LLM 호출 model (예: 'qwen-7b').
        pii_service: ADR-020 Layer 1·4. None이면 PII 검사 건너뜀.
        chat_log_writer: 스트림 완료 후 저장. None이면 저장 생략(테스트 호환).
        query_classifier: ADR-013 §3 — Tier 1 정규식 + Tier 2 LLM 분류. None이면 default.
        model_router: ADR-013 §1·§2 — routing.yaml 룰 평가. None이면 default tenant_slm.
    """

    def __init__(
        self,
        *,
        llm: LLMClient,
        prompt: StreamingPrompt,
        default_model: str,
        pii_service: PIIService | None = None,
        chat_log_writer: ChatLogWriter | None = None,
        query_classifier: QueryClassifier | None = None,
        model_router: ModelRouter | None = None,
        ledger_audit=None,
    ) -> None:
        self._llm = llm
        self._prompt = prompt
        self._default_model = default_model
        self._pii = pii_service
        self._chat_log_writer = chat_log_writer
        self._classifier = query_classifier
        self._router = model_router
        # ADR-020 §8 — input_pii_blocked 시 publish_pii_high_severity_block.
        self._ledger = ledger_audit

    async def stream(
        self,
        *,
        domain_id: str,
        user_id: str,
        question: str,
        tenant_config: dict[str, Any] | None,
        conversation_id: str | None = None,
        request_id: str | None = None,
        selected_model: str | None = None,
        selected_lora: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.5,
    ) -> AsyncIterator[StreamEvent]:
        """SSE 이벤트 stream. caller(backend endpoint)가 각 이벤트를 SSE 형식으로 직렬화."""
        # message_id == request_id — Feedback API가 동일 ID로 chat_logs를 식별
        # (ADR-017 §5 + §3.2 complete payload).
        # conversation_id는 응답·chat_logs.conversation_id가 일치하도록 upfront 발급
        # — Conversation API(ADR-017 §4)가 같은 ID로 lookup 가능.
        request_id = request_id or uuid.uuid4().hex
        message_id = request_id
        conversation_id = conversation_id or uuid.uuid4().hex
        start = time.perf_counter()

        # ----- Layer 1·2 PII check ---------------------------------------- #
        input_pii_found: list[dict[str, Any]] = []
        storage_policy = "mask"
        question_for_log = question
        if self._pii is not None:
            pii_cfg = (tenant_config or {}).get("pii")
            check = self._pii.check_input(question, pii_cfg)
            input_pii_found = check.findings
            plain_approved = bool(
                ((pii_cfg or {}).get("storage") or {}).get("plain_approved", False)
            )
            storage = self._pii.mask_for_storage(
                question,
                pii_cfg,
                compliance_mode=str(
                    (tenant_config or {}).get("compliance_mode") or "standard"
                ),
                plain_approved=plain_approved,
            )
            storage_policy = storage.policy
            if storage.policy == "mask":
                question_for_log = storage.text
            if check.blocked:
                blocked_message = (
                    "개인정보로 보이는 정보가 포함되어 있습니다. "
                    "민감한 정보를 제거하고 다시 시도해 주세요."
                )
                latency_ms = int((time.perf_counter() - start) * 1000)
                await self._save_log(
                    domain_id=domain_id,
                    request_id=request_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    question=question_for_log,
                    answer=blocked_message,
                    selected_model=selected_model,
                    selected_lora=selected_lora,
                    fallback_reason="input_pii_blocked",
                    input_pii_found=input_pii_found,
                    output_pii_masked=[],
                    pii_storage_policy=storage_policy,
                    latency_ms=latency_ms,
                )
                # ADR-020 §8 — Ledger publish (best-effort, swallow on failure)
                if self._ledger is not None:
                    try:
                        await self._ledger.publish_pii_high_severity_block(
                            domain_id=domain_id,
                            actor=user_id,
                            categories=list(check.blocked_categories),
                            details={
                                "request_id": request_id,
                                "ui_mode": "chat_streaming",
                            },
                        )
                    except Exception:  # noqa: BLE001
                        pass
                yield StreamEvent(
                    event="fallback",
                    data={
                        "reason": "input_pii_blocked",
                        "blocked_categories": check.blocked_categories,
                        "message": blocked_message,
                        "message_id": message_id,
                        "conversation_id": conversation_id,
                    },
                )
                return

        # ----- Classify + Route (ADR-013 §1·§3) --------------------------- #
        classifier_decision: dict[str, Any] = {}
        routing_decision: dict[str, Any] = {}
        if self._classifier is not None:
            classifier_config = ClassifierConfig.from_dict(
                (tenant_config or {}).get("query_classifier")
            )
            cls_result = await self._classifier.classify(
                question=question, config=classifier_config
            )
            classifier_decision = cls_result.to_log_dict()
        else:
            cls_result = ClassificationResult(
                query_type="free_chat", support_type=None, complexity=None
            )
            classifier_decision = {"skipped": True, "query_type": "free_chat"}

        if self._router is not None:
            routing_cfg = (tenant_config or {}).get("routing")
            tenant_model_cfg = (tenant_config or {}).get("model") or {}
            decision = self._router.decide(
                classification=cls_result,
                routing_config=routing_cfg,
                tenant_model_config=tenant_model_cfg,
            )
            selected_model = decision.model or selected_model
            selected_lora = decision.lora_adapter or selected_lora
            routing_decision = {
                "matched_rule": decision.matched_rule,
                "selected_model": decision.model,
                "selected_lora": decision.lora_adapter,
                "use_rag": decision.use_rag,
                "ui_mode": decision.ui_mode,
            }
            # routing.action=fallback_refusal 시 LLM 호출 없이 단축 fallback
            if decision.action:
                latency_ms = int((time.perf_counter() - start) * 1000)
                refusal_message = (
                    "현재 정책상 답변을 드릴 수 없는 요청입니다."
                )
                await self._save_log(
                    domain_id=domain_id,
                    request_id=request_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    question=question_for_log,
                    answer=refusal_message,
                    selected_model=selected_model,
                    selected_lora=selected_lora,
                    fallback_reason=f"routing_{decision.action}",
                    input_pii_found=input_pii_found,
                    output_pii_masked=[],
                    pii_storage_policy=storage_policy,
                    classifier_decision=classifier_decision,
                    routing_decision=routing_decision,
                    latency_ms=latency_ms,
                )
                yield StreamEvent(
                    event="fallback",
                    data={
                        "reason": f"routing_{decision.action}",
                        "message": refusal_message,
                        "message_id": message_id,
                        "conversation_id": conversation_id,
                    },
                )
                return
        else:
            routing_decision = {
                "matched_rule": "default_skipped",
                "selected_model": selected_model or "tenant_slm",
                "selected_lora": selected_lora,
                "use_rag": False,
                "ui_mode": "chat_streaming",
            }

        # ----- LLM stream ------------------------------------------------- #
        prompt = self._prompt.render(question)
        model = selected_model or self._default_model
        accumulated: list[str] = []
        try:
            async for token in self._llm.stream(
                prompt=prompt,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                lora_adapter=selected_lora,
            ):
                if not token:
                    continue
                accumulated.append(token)
                yield StreamEvent(event="token", data={"text": token})
        except Exception as e:  # noqa: BLE001 — graceful fallback
            latency_ms = int((time.perf_counter() - start) * 1000)
            await self._save_log(
                domain_id=domain_id,
                request_id=request_id,
                user_id=user_id,
                conversation_id=conversation_id,
                question=question_for_log,
                answer="".join(accumulated),
                selected_model=selected_model,
                selected_lora=selected_lora,
                fallback_reason=f"llm_error:{type(e).__name__}",
                input_pii_found=input_pii_found,
                output_pii_masked=[],
                pii_storage_policy=storage_policy,
                classifier_decision=classifier_decision,
                routing_decision=routing_decision,
                latency_ms=latency_ms,
            )
            yield StreamEvent(
                event="error",
                data={"reason": "llm_error", "detail": type(e).__name__},
            )
            return

        # ----- Layer 4 PII mask + chat_logs ------------------------------- #
        full_text = "".join(accumulated)
        output_pii_masked: list[dict[str, Any]] = []
        if self._pii is not None and full_text:
            pii_cfg = (tenant_config or {}).get("pii")
            masked = self._pii.mask_output(full_text, pii_cfg)
            full_text = masked.masked_text
            output_pii_masked = masked.findings

        latency_ms = int((time.perf_counter() - start) * 1000)
        conv_id = await self._save_log(
            domain_id=domain_id,
            request_id=request_id,
            user_id=user_id,
            conversation_id=conversation_id,
            question=question_for_log,
            answer=full_text,
            selected_model=selected_model,
            selected_lora=selected_lora,
            fallback_reason=None,
            input_pii_found=input_pii_found,
            output_pii_masked=output_pii_masked,
            pii_storage_policy=storage_policy,
            classifier_decision=classifier_decision,
            routing_decision=routing_decision,
            latency_ms=latency_ms,
        )
        yield StreamEvent(
            event="complete",
            data={
                "message_id": message_id,
                "conversation_id": conv_id or conversation_id,
                "metadata": {
                    "ui_mode": "chat_streaming",
                    "llm_model": selected_model,
                    "lora_adapter": selected_lora,
                    "latency_ms": latency_ms,
                    "pii": {
                        "input_pii_found": input_pii_found,
                        "output_pii_masked": output_pii_masked,
                    },
                    "classifier_decision": classifier_decision,
                    "routing_decision": routing_decision,
                    "citation_disabled": True,
                },
            },
        )

    # ------------------------------------------------------------------ #

    async def _save_log(
        self,
        *,
        domain_id: str,
        request_id: str,
        user_id: str,
        conversation_id: str | None,
        question: str,
        answer: str,
        selected_model: str | None,
        selected_lora: str | None,
        fallback_reason: str | None,
        input_pii_found: list[dict[str, Any]],
        output_pii_masked: list[dict[str, Any]],
        pii_storage_policy: str,
        latency_ms: int,
        classifier_decision: dict[str, Any] | None = None,
        routing_decision: dict[str, Any] | None = None,
    ) -> str | None:
        if self._chat_log_writer is None:
            return None
        payload = ChatLogPayload(
            domain_id=domain_id,
            request_id=request_id,
            user_id=user_id,
            conversation_id=conversation_id,
            question=question,
            answer=answer,
            retrieved_chunks=[],
            citations=[],
            citation_types=[],
            llm_model=selected_model,
            lora_adapter=selected_lora,
            ui_mode="chat_streaming",
            confidence=0.0,
            fallback_reason=fallback_reason,
            unsupported_ratio=0.0,
            verifier_metrics={},
            routing_decision=routing_decision or {
                "selected_model": selected_model,
                "selected_lora": selected_lora,
                "use_rag": False,
                "ui_mode": "chat_streaming",
            },
            classifier_decision=classifier_decision or {},
            input_pii_found=input_pii_found,
            output_pii_masked=output_pii_masked,
            pii_storage_policy=pii_storage_policy,
            latency_ms=latency_ms,
        )
        return await self._chat_log_writer.write(payload)
