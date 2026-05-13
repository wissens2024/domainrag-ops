"""ChatLogWriter — chat_logs INSERT (audit truth) Protocol.

ADR-013 §10 chat_logs 4 컬럼 + ADR-019 partitioning + ADR-020 PII 컬럼 + ADR-008 RLS
를 모두 만족하는 단일 진입점. CLAUDE.md 원칙 9 (모든 응답 저장) 와 원칙 10
(excerpt가 audit truth)을 보장한다.

본 모듈은 rag_core 레벨에서 SQLAlchemy 의존 없는 Protocol만 정의. 운영 구현
(Postgres + RLS)은 backend/app/services/chat_log_writer.py.

Protocol 계약:
  - write(payload) → conversation_id (auto-create 또는 caller가 미리 발급한 값)
  - tenant_id 격리(RLS) 책임은 구현체에 있다
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ChatLogPayload:
    """chat_logs 1행에 들어갈 모든 필드 (ADR-019 §2 + ADR-013 §10 + ADR-020).

    구현체는 본 dataclass의 모든 필드를 column에 매핑한다. None 또는 빈 값은
    SQL DEFAULT를 활용한다.
    """

    tenant_id: str
    request_id: str
    user_id: str | None
    conversation_id: str | None  # None → writer가 conversations row를 자동 생성
    question: str
    answer: str

    # 검색·생성 산출 (excerpt 포함 — audit truth)
    retrieved_chunks: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    citation_types: list[str] = field(default_factory=list)
    rewritten_query: str | None = None

    # 모델·라우팅 메타
    llm_model: str | None = None
    embedding_model: str | None = None
    reranker_model: str | None = None
    prompt_version: str | None = None
    lora_adapter: str | None = None
    ui_mode: str = "chat_structured"

    # 검증 메타
    confidence: float = 0.0
    fallback_reason: str | None = None
    unsupported_ratio: float = 0.0
    verifier_metrics: dict[str, Any] = field(default_factory=dict)
    routing_decision: dict[str, Any] = field(default_factory=dict)
    classifier_decision: dict[str, Any] = field(default_factory=dict)
    model_failure_chain: list[dict[str, Any]] = field(default_factory=list)
    inference_judge_results: list[dict[str, Any]] = field(default_factory=list)
    conflict_groups: list[Any] = field(default_factory=list)

    # PII (ADR-020)
    input_pii_found: list[dict[str, Any]] = field(default_factory=list)
    output_pii_masked: list[dict[str, Any]] = field(default_factory=list)
    pii_storage_policy: str = "mask"

    # 사용자 피드백 (ADR-017 §5) — POST /feedback이 사후에 UPDATE
    feedback: str | None = None
    feedback_comment: str | None = None

    # 타이밍
    latency_ms: int | None = None


class ChatLogWriter(Protocol):
    """chat_logs INSERT 인터페이스. 구현체:
      - PostgresChatLogWriter (backend, SQLAlchemy + RLS)
      - InMemoryChatLogWriter (테스트)
    """

    async def write(self, payload: ChatLogPayload) -> str:
        """chat_logs 행을 저장하고 conversation_id를 반환.

        payload.conversation_id가 None이면 conversations row를 자동 생성하여
        새 id를 반환한다. 그 외에는 받은 값을 그대로 반환.
        """
        ...


class InMemoryChatLogWriter:
    """테스트·dev 모드용. 모든 payload를 records 리스트에 append.

    conversation_id 자동 발급은 단순 카운터로 흉내. 운영에서는 PostgreSQL
    gen_random_uuid()를 사용한다.
    """

    def __init__(self) -> None:
        self.records: list[ChatLogPayload] = []
        self._conv_counter = 0

    async def write(self, payload: ChatLogPayload) -> str:
        if payload.conversation_id is None:
            self._conv_counter += 1
            payload = ChatLogPayload(**{**payload.__dict__,
                                        "conversation_id": f"conv-{self._conv_counter:08d}"})
        self.records.append(payload)
        return payload.conversation_id  # type: ignore[return-value]
