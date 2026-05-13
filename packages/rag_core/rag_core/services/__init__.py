"""rag_core domain services — Protocol 어댑터들을 묶어 흐름을 만든다.

운영용:
  - build_qdrant_acl_filter (ADR-004 + ADR-008 + ADR-018)
  - RetrievalService (ADR-011)
  - GenerationService (ADR-010 §2 hybrid structured)
"""

from .acl_builder import build_qdrant_acl_filter
from .chat_log_writer import (
    ChatLogPayload,
    ChatLogWriter,
    InMemoryChatLogWriter,
)
from .conflict_detector import ConflictDetectionResult, ConflictDetector
from .generation_service import GenerationResult, GenerationService
from .judge_service import JudgePrompt, JudgeResult, JudgeService
from .model_router import ModelRouter, RoutingDecision
from .pii_service import (
    InputPIICheck,
    OutputPIIMask,
    PIIService,
    StoragePIIDecision,
)
from .query_rewriter import QueryRewriter, QueryRewritePrompt, RewriteResult
from .streaming_chat_service import (
    StreamEvent,
    StreamingChatService,
    StreamingPrompt,
)
from .query_classifier import (
    ClassificationResult,
    ClassifierConfig,
    ClassifierTier2Prompt,
    QueryClassifier,
)
from .retrieval_service import RetrievalService
from .verifier_service import (
    VerificationResult,
    VerifiedCitation,
    VerifierService,
    VerifierThresholds,
)

__all__ = [
    "build_qdrant_acl_filter",
    "RetrievalService",
    "GenerationService",
    "GenerationResult",
    "VerifierService",
    "VerifierThresholds",
    "VerificationResult",
    "VerifiedCitation",
    "JudgeService",
    "JudgePrompt",
    "JudgeResult",
    "ChatLogWriter",
    "ChatLogPayload",
    "InMemoryChatLogWriter",
    "QueryClassifier",
    "ClassifierConfig",
    "ClassifierTier2Prompt",
    "ClassificationResult",
    "ModelRouter",
    "RoutingDecision",
    "PIIService",
    "InputPIICheck",
    "OutputPIIMask",
    "StoragePIIDecision",
    "ConflictDetector",
    "ConflictDetectionResult",
    "QueryRewriter",
    "QueryRewritePrompt",
    "RewriteResult",
    "StreamingChatService",
    "StreamingPrompt",
    "StreamEvent",
]
