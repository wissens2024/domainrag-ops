"""PIIService.check_chunk_pii — ADR-020 §5 Layer 3 인덱싱 PII 검사."""

from __future__ import annotations

from pathlib import Path

import pytest

from rag_core.pii import RegexPIIDetector
from rag_core.services.pii_service import PIIService

RULES_DIR = Path(__file__).resolve().parents[1] / "rag_core" / "pii" / "rules"


@pytest.fixture
def pii_service() -> PIIService:
    return PIIService(detector=RegexPIIDetector(rules_dir=RULES_DIR))


def _config(**overrides) -> dict:
    base = {
        "indexing": {
            "enable": True,
            "on_pii_found_in_chunk": {
                "severity_threshold": "medium",
                "block_indexing": False,
            },
        },
        "severity_map": {
            "rrn": "high",
            "credit_card": "high",
            "phone": "medium",
            "email": "low",
        },
    }
    for k, v in overrides.items():
        base[k] = v
    return base


def test_high_severity_recorded(pii_service: PIIService):
    result = pii_service.check_chunk_pii(
        "예시 주민번호 901231-1234567 가 본문에 포함됨", _config()
    )
    assert any(w["category"] == "rrn" and w["severity"] == "high"
               for w in result.pii_warnings)
    assert result.has_high_severity is True
    # block_indexing flag가 false면 high여도 block_indexing=False
    assert result.block_indexing is False


def test_medium_threshold_filters_low_severity(pii_service: PIIService):
    """severity_threshold=medium → email(low)는 warning에 안 들어감."""
    result = pii_service.check_chunk_pii(
        "문의: user@example.com 로 연락 바랍니다", _config()
    )
    # email은 low → 제외
    assert all(w["category"] != "email" for w in result.pii_warnings)


def test_phone_medium_included_at_medium_threshold(pii_service: PIIService):
    result = pii_service.check_chunk_pii(
        "고객 연락처 010-1234-5678", _config()
    )
    assert any(w["category"] == "phone" and w["severity"] == "medium"
               for w in result.pii_warnings)
    assert result.has_high_severity is False


def test_low_threshold_includes_email(pii_service: PIIService):
    cfg = _config()
    cfg["indexing"]["on_pii_found_in_chunk"]["severity_threshold"] = "low"
    result = pii_service.check_chunk_pii(
        "문의 user@example.com", cfg
    )
    assert any(w["category"] == "email" for w in result.pii_warnings)


def test_block_indexing_only_for_high(pii_service: PIIService):
    cfg = _config()
    cfg["indexing"]["on_pii_found_in_chunk"]["block_indexing"] = True
    # phone(medium)만 → block_indexing=False
    r1 = pii_service.check_chunk_pii("010-1234-5678", cfg)
    assert r1.block_indexing is False
    # rrn(high) → block_indexing=True
    r2 = pii_service.check_chunk_pii("901231-1234567", cfg)
    assert r2.block_indexing is True


def test_disabled_layer3_returns_empty(pii_service: PIIService):
    cfg = _config()
    cfg["indexing"]["enable"] = False
    result = pii_service.check_chunk_pii("901231-1234567", cfg)
    assert result.pii_warnings == []
    assert result.has_high_severity is False


def test_clean_content_returns_empty(pii_service: PIIService):
    result = pii_service.check_chunk_pii(
        "본 문서는 정보보안 정책 요약입니다.", _config()
    )
    assert result.pii_warnings == []
    assert result.has_high_severity is False


def test_severity_map_overrides_rule_severity(pii_service: PIIService):
    """severity_map으로 email을 high로 올리면 medium threshold에도 잡힘."""
    cfg = _config()
    cfg["severity_map"]["email"] = "high"
    result = pii_service.check_chunk_pii(
        "문의 user@example.com", cfg
    )
    assert any(w["category"] == "email" and w["severity"] == "high"
               for w in result.pii_warnings)
    assert result.has_high_severity is True


def test_warnings_have_jsonb_friendly_form(pii_service: PIIService):
    """chunks.pii_warnings JSONB 적재 가능한 dict 형식."""
    result = pii_service.check_chunk_pii("901231-1234567", _config())
    for w in result.pii_warnings:
        assert set(w.keys()) >= {"category", "severity", "position", "masked_form", "action"}
        assert w["action"] == "warn"
        assert isinstance(w["position"], list) and len(w["position"]) == 2
