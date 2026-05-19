"""ADR-013 §3 + ADR-010 §2 공식 enum 검증."""

from rag_core.enums import (
    QUERY_TYPES,
    SUPPORT_TYPES,
    SUPPORT_LEVELS,
    UI_MODES,
    COMPLEXITY_LEVELS,
    QueryTypeEnum,
    SupportTypeEnum,
    SupportLevelEnum,
    UiModeEnum,
    ComplexityEnum,
    is_valid_query_type,
    is_valid_support_type,
    is_valid_support_level,
    is_valid_ui_mode,
    is_valid_complexity,
)


def test_query_types_match_adr_013_table():
    assert QUERY_TYPES == frozenset({
        "document_qa",
        "assessment_extract",
        "assessment_generate",
        "assessment_hybrid",
        "free_chat",
        "meta",
    })


def test_complexity_levels():
    assert COMPLEXITY_LEVELS == frozenset({"low", "medium", "high"})


def test_support_types_match_adr_010():
    assert SUPPORT_TYPES == frozenset({"direct", "synthesis", "inference", "conflict"})


def test_support_levels():
    assert SUPPORT_LEVELS == frozenset({"strong", "medium", "weak"})


def test_ui_modes():
    assert UI_MODES == frozenset({"chat_structured", "chat_streaming"})


def test_validators_accept_valid_values():
    assert is_valid_query_type("document_qa")
    assert is_valid_support_type("inference")
    assert is_valid_support_level("strong")
    assert is_valid_ui_mode("chat_streaming")
    assert is_valid_complexity("medium")


def test_validators_reject_invalid_values():
    assert not is_valid_query_type("unknown_type")
    assert not is_valid_support_type("DIRECT")  # uppercase 거절
    assert not is_valid_support_level("very_strong")
    assert not is_valid_ui_mode("chat_voice")
    assert not is_valid_complexity("LOW")


def test_enum_value_str_dual_inheritance():
    """Enum이 str을 상속해 직렬화 시 value 그대로 노출."""
    assert QueryTypeEnum.DOCUMENT_QA == "document_qa"
    assert SupportTypeEnum.INFERENCE.value == "inference"
    assert SupportLevelEnum.STRONG.value == "strong"
    assert UiModeEnum.CHAT_STRUCTURED == "chat_structured"
    assert ComplexityEnum.HIGH.value == "high"
