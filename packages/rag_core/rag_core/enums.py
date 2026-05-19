"""ADR-013 §3 + ADR-010 §2 공식 enum 단일 진실 소스.

본 모듈은 routing rule (`when:`), citation metadata, chat_logs, frontend types.ts가
공유하는 enum을 Python Literal/Enum으로 정의한다. 신규 값을 추가하려면 본 ADR-013 §3
표 + frontend types.ts + 본 모듈을 동시 갱신.

Naming: snake_case lowercase. Enum class member name(`DIRECT`)과 value(`'direct'`)
분리 — 직렬화는 항상 value로.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal


# --------------------------------------------------------------------------- #
# Literal aliases (가벼운 type hint용 — pydantic·dataclass 필드에서 사용)
# --------------------------------------------------------------------------- #

QueryType = Literal[
    "document_qa",
    "assessment_extract",
    "assessment_generate",
    "assessment_hybrid",
    "free_chat",
    "meta",
]

Complexity = Literal["low", "medium", "high"]

SupportType = Literal["direct", "synthesis", "inference", "conflict"]
"""ADR-010 §2 — 4-type citation."""

SupportLevel = Literal["strong", "medium", "weak"]
"""ADR-010 §5 — Tier 2 cosine similarity 기반 분류."""

UiMode = Literal["chat_structured", "chat_streaming"]
"""ADR-013 §6."""


# --------------------------------------------------------------------------- #
# Enum (Python Enum API가 필요한 곳용)
# --------------------------------------------------------------------------- #


class QueryTypeEnum(str, Enum):
    DOCUMENT_QA = "document_qa"
    ASSESSMENT_EXTRACT = "assessment_extract"
    ASSESSMENT_GENERATE = "assessment_generate"
    ASSESSMENT_HYBRID = "assessment_hybrid"
    FREE_CHAT = "free_chat"
    META = "meta"


class ComplexityEnum(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SupportTypeEnum(str, Enum):
    DIRECT = "direct"
    SYNTHESIS = "synthesis"
    INFERENCE = "inference"
    CONFLICT = "conflict"


class SupportLevelEnum(str, Enum):
    STRONG = "strong"
    MEDIUM = "medium"
    WEAK = "weak"


class UiModeEnum(str, Enum):
    CHAT_STRUCTURED = "chat_structured"
    CHAT_STREAMING = "chat_streaming"


# --------------------------------------------------------------------------- #
# Frozen sets (검증·필터링용)
# --------------------------------------------------------------------------- #

QUERY_TYPES: frozenset[str] = frozenset(e.value for e in QueryTypeEnum)
COMPLEXITY_LEVELS: frozenset[str] = frozenset(e.value for e in ComplexityEnum)
SUPPORT_TYPES: frozenset[str] = frozenset(e.value for e in SupportTypeEnum)
SUPPORT_LEVELS: frozenset[str] = frozenset(e.value for e in SupportLevelEnum)
UI_MODES: frozenset[str] = frozenset(e.value for e in UiModeEnum)


def is_valid_query_type(value: str) -> bool:
    return value in QUERY_TYPES


def is_valid_support_type(value: str) -> bool:
    return value in SUPPORT_TYPES


def is_valid_support_level(value: str) -> bool:
    return value in SUPPORT_LEVELS


def is_valid_ui_mode(value: str) -> bool:
    return value in UI_MODES


def is_valid_complexity(value: str) -> bool:
    return value in COMPLEXITY_LEVELS
