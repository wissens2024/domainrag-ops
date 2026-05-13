"""PIIService unit tests — ADR-020 §3 Layer 1 + §6 Layer 4 정책 검증.

RegexPIIDetector를 실제 룰(yaml)로 로드해 정책 layer만 검증한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag_core.pii import RegexPIIDetector
from rag_core.services.pii_service import PIIService

RULES_DIR = Path(__file__).resolve().parents[1] / "rag_core" / "pii" / "rules"


@pytest.fixture
def pii_service() -> PIIService:
    return PIIService(detector=RegexPIIDetector(rules_dir=RULES_DIR))


def _platform_pii_config(**overrides) -> dict:
    """configs/platform/pii.yaml의 기본 정책 형태 (테스트용 minimal)."""
    base = {
        "input": {
            "enable": True,
            "on_pii_found": {
                "high_severity": "block",
                "medium_severity": "warn",
                "low_severity": "log",
            },
        },
        "response": {"enable": True},
        "severity_map": {
            "rrn": "high",
            "credit_card": "high",
            "api_key": "high",
            "phone": "medium",
            "email": "low",
            "ip_address": "low",
        },
    }
    for k, v in overrides.items():
        base[k] = v
    return base


# --------------------------------------------------------------------------- #
# Layer 1 — check_input
# --------------------------------------------------------------------------- #


def test_high_severity_rrn_blocks_input(pii_service: PIIService) -> None:
    cfg = _platform_pii_config()
    result = pii_service.check_input(
        "제 주민번호는 901231-1234567 입니다", cfg
    )
    assert result.blocked is True
    assert "rrn" in result.blocked_categories
    assert any(f["category"] == "rrn" and f["action"] == "block"
               for f in result.findings)


def test_medium_severity_phone_warns_but_allows(pii_service: PIIService) -> None:
    cfg = _platform_pii_config()
    result = pii_service.check_input("연락처는 010-1234-5678 입니다", cfg)
    assert result.blocked is False
    assert any(f["category"] == "phone" and f["action"] == "warn"
               for f in result.findings)


def test_low_severity_email_logs_only(pii_service: PIIService) -> None:
    cfg = _platform_pii_config()
    result = pii_service.check_input("문의는 user@example.com 으로", cfg)
    assert result.blocked is False
    assert any(f["category"] == "email" and f["action"] == "log"
               for f in result.findings)


def test_clean_input_yields_empty_findings(pii_service: PIIService) -> None:
    cfg = _platform_pii_config()
    result = pii_service.check_input("패스워드 정책은?", cfg)
    assert result.blocked is False
    assert result.findings == []
    assert result.blocked_categories == []


def test_disabled_layer1_skips_detection(pii_service: PIIService) -> None:
    cfg = _platform_pii_config(input={"enable": False})
    result = pii_service.check_input(
        "제 주민번호는 901231-1234567 입니다", cfg
    )
    assert result.blocked is False
    assert result.findings == []


def test_severity_map_overrides_rule_default(pii_service: PIIService) -> None:
    """email은 룰 yaml에서 severity=low지만 severity_map으로 high로 올리면 block."""
    cfg = _platform_pii_config()
    cfg["severity_map"]["email"] = "high"
    result = pii_service.check_input("user@example.com", cfg)
    assert result.blocked is True
    assert "email" in result.blocked_categories


def test_block_collects_unique_categories(pii_service: PIIService) -> None:
    """RRN + credit card는 둘 다 high → block. account_number 룰이 카드번호와 겹쳐도
    high인 한 모두 blocked_categories에 누적되며, 중복은 제거된다."""
    cfg = _platform_pii_config()
    result = pii_service.check_input(
        "주민 901231-1234567 카드 4111-1111-1111-1111", cfg
    )
    assert result.blocked is True
    cats = set(result.blocked_categories)
    assert {"rrn", "credit_card"} <= cats
    # 중복 없음 (정렬된 리스트 = sorted unique)
    assert sorted(set(result.blocked_categories)) == result.blocked_categories


# --------------------------------------------------------------------------- #
# Layer 4 — mask_output
# --------------------------------------------------------------------------- #


def test_mask_replaces_rrn_in_response(pii_service: PIIService) -> None:
    cfg = _platform_pii_config()
    text = "예시: 901231-1234567 형식으로 작성"
    result = pii_service.mask_output(text, cfg)
    assert "901231-1234567" not in result.masked_text
    assert "예시:" in result.masked_text and "형식으로 작성" in result.masked_text
    assert any(f["category"] == "rrn" for f in result.findings)


def test_mask_disabled_layer4_returns_text_unchanged(
    pii_service: PIIService,
) -> None:
    cfg = _platform_pii_config(response={"enable": False})
    result = pii_service.mask_output("주민 901231-1234567", cfg)
    assert result.masked_text == "주민 901231-1234567"
    assert result.findings == []


def test_mask_empty_text_no_findings(pii_service: PIIService) -> None:
    cfg = _platform_pii_config()
    result = pii_service.mask_output("", cfg)
    assert result.masked_text == ""
    assert result.findings == []


def test_mask_clean_text_no_findings(pii_service: PIIService) -> None:
    cfg = _platform_pii_config()
    result = pii_service.mask_output("패스워드는 12자 이상이어야 합니다", cfg)
    assert result.findings == []


# --------------------------------------------------------------------------- #
# Layer 2 — mask_for_storage (chat_logs 보관 정책)
# --------------------------------------------------------------------------- #


def test_storage_default_policy_is_mask(pii_service: PIIService) -> None:
    cfg = _platform_pii_config()
    cfg["storage"] = {}  # pii_storage_policy 미지정 — default 'mask'
    decision = pii_service.mask_for_storage(
        "주민 901231-1234567", cfg, compliance_mode="standard"
    )
    assert decision.policy == "mask"
    assert "901231-1234567" not in decision.text
    assert any(f["category"] == "rrn" for f in decision.findings)
    assert decision.compliance_forced is False


def test_storage_plain_without_approval_falls_back_to_mask(
    pii_service: PIIService,
) -> None:
    """ADR-020 §4 — configured=plain이지만 platform_admin 승인 없으면 mask로 fallback."""
    cfg = _platform_pii_config()
    cfg["storage"] = {"pii_storage_policy": "plain"}
    decision = pii_service.mask_for_storage(
        "주민 901231-1234567", cfg, compliance_mode="standard"
    )
    assert decision.policy == "mask"
    assert decision.plain_approval_missing is True
    assert "901231-1234567" not in decision.text


def test_storage_plain_with_approval_keeps_raw_under_standard_compliance(
    pii_service: PIIService,
) -> None:
    """ADR-020 §4 — plain_approved=True면 원문 보관."""
    cfg = _platform_pii_config()
    cfg["storage"] = {"pii_storage_policy": "plain"}
    decision = pii_service.mask_for_storage(
        "주민 901231-1234567",
        cfg,
        compliance_mode="standard",
        plain_approved=True,
    )
    assert decision.policy == "plain"
    assert decision.text == "주민 901231-1234567"
    assert decision.findings == []
    assert decision.plain_approval_missing is False


def test_storage_gdpr_strict_forces_mask_even_when_plain_configured(
    pii_service: PIIService,
) -> None:
    """ADR-020 §10 — compliance_mode가 gdpr_strict면 승인이 있어도 mask 강제."""
    cfg = _platform_pii_config()
    cfg["storage"] = {"pii_storage_policy": "plain"}
    decision = pii_service.mask_for_storage(
        "주민 901231-1234567",
        cfg,
        compliance_mode="gdpr_strict",
        plain_approved=True,
    )
    assert decision.policy == "mask"
    assert decision.compliance_forced is True
    assert "901231-1234567" not in decision.text


def test_storage_hipaa_strict_forces_mask(pii_service: PIIService) -> None:
    cfg = _platform_pii_config()
    cfg["storage"] = {"pii_storage_policy": "plain"}
    decision = pii_service.mask_for_storage(
        "주민 901231-1234567",
        cfg,
        compliance_mode="hipaa_strict",
        plain_approved=True,
    )
    assert decision.policy == "mask"
    assert decision.compliance_forced is True


def test_storage_empty_text_safe(pii_service: PIIService) -> None:
    cfg = _platform_pii_config()
    decision = pii_service.mask_for_storage("", cfg)
    assert decision.text == ""
    assert decision.findings == []


def test_storage_no_pii_in_text(pii_service: PIIService) -> None:
    cfg = _platform_pii_config()
    decision = pii_service.mask_for_storage(
        "패스워드 정책은?", cfg, compliance_mode="standard"
    )
    assert decision.policy == "mask"  # 정책은 mask지만 마스킹 대상 없음
    assert decision.text == "패스워드 정책은?"
    assert decision.findings == []


def test_mask_findings_carry_severity_from_map(pii_service: PIIService) -> None:
    cfg = _platform_pii_config()
    cfg["severity_map"]["phone"] = "high"
    result = pii_service.mask_output("연락처 010-1234-5678", cfg)
    phone_findings = [f for f in result.findings if f["category"] == "phone"]
    assert phone_findings, "phone finding 누락"
    assert all(f["severity"] == "high" for f in phone_findings)
