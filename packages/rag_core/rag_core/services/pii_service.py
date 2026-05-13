"""PIIService — ADR-020 Layer 1·2·3·4 정책 결합.

PIIDetector(보통 RegexPIIDetector)는 raw scan/mask만 담당. 본 서비스는
configs/platform/pii.yaml + tenant overrides의 정책(severity_map / on_pii_found /
response.enable / indexing.severity_threshold)을 적용해 노드·인덱싱 파이프라인이
바로 쓸 수 있는 dataclass를 반환한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rag_core.interfaces.pii import PIIDetector, PIIFinding


@dataclass
class InputPIICheck:
    findings: list[dict[str, Any]] = field(default_factory=list)
    blocked: bool = False
    blocked_categories: list[str] = field(default_factory=list)


@dataclass
class OutputPIIMask:
    masked_text: str
    findings: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class StoragePIIDecision:
    """ADR-020 §4 — chat_logs 보관 시 적용된 PII 정책."""

    policy: str  # 'mask' | 'plain'
    text: str    # 적용 후 보관될 question 텍스트 (mask면 마스킹된 form)
    findings: list[dict[str, Any]] = field(default_factory=list)
    compliance_forced: bool = False  # gdpr_strict/hipaa_strict 강제 적용 여부
    plain_approval_missing: bool = False  # configured=plain이지만 platform_admin 승인 없음 → mask로 fallback


@dataclass
class ChunkPIIWarning:
    """ADR-020 §5 — 인덱싱 시 chunk content PII 검사 결과."""

    pii_warnings: list[dict[str, Any]] = field(default_factory=list)  # chunks.pii_warnings JSONB 적재 형식
    has_high_severity: bool = False
    block_indexing: bool = False  # severity 기준 + indexing.block_indexing 플래그 결합


_SEV_RANK = {"low": 1, "medium": 2, "high": 3}


_ACTION_KEYS = {
    "high": "high_severity",
    "medium": "medium_severity",
    "low": "low_severity",
}


def _resolve_severity(category: str, default: str, severity_map: dict[str, Any]) -> str:
    """severity_map override → detector severity 순. 알 수 없는 값은 'medium'."""
    sev = severity_map.get(category, default)
    if sev not in {"low", "medium", "high"}:
        return "medium"
    return sev


def _resolve_action(severity: str, on_pii_found: dict[str, Any]) -> str:
    """ADR-020 §3 — high→block, medium→warn, low→log default. yaml override 가능."""
    key = _ACTION_KEYS.get(severity, "medium_severity")
    action = on_pii_found.get(key, "log" if severity == "low" else
                              ("warn" if severity == "medium" else "block"))
    if action not in {"block", "warn", "log"}:
        return "log"
    return action


def _finding_to_dict(
    finding: PIIFinding,
    *,
    severity: str,
    action: str,
) -> dict[str, Any]:
    return {
        "category": finding.category,
        "severity": severity,
        "position": [finding.position[0], finding.position[1]],
        "masked_form": finding.masked_form,
        "action": action,
    }


class PIIService:
    """ADR-020 §3 (Layer 1) + §6 (Layer 4) 정책 wrapper.

    detector: PIIDetector 구현체 (RegexPIIDetector / 테스트 mock 등)
    """

    def __init__(self, detector: PIIDetector) -> None:
        self.detector = detector

    def check_input(
        self, question: str, pii_config: dict[str, Any] | None
    ) -> InputPIICheck:
        """ADR-020 §3 — 입력 PII 감지·정책 적용.

        반환:
          - findings: 발견된 PII 메타(원문 X, 마스킹 form만 — chat_logs 적재용)
          - blocked: 하나 이상이 action=block이면 True
          - blocked_categories: block 유발 카테고리 (정렬·중복 제거)
        """
        cfg = pii_config or {}
        input_cfg = cfg.get("input") or {}
        if not input_cfg.get("enable", True):
            return InputPIICheck()

        severity_map = cfg.get("severity_map") or {}
        on_pii_found = input_cfg.get("on_pii_found") or {}

        raw = self.detector.scan(question or "")
        findings: list[dict[str, Any]] = []
        blocked = False
        blocked_cats: set[str] = set()
        for f in raw:
            sev = _resolve_severity(f.category, f.severity, severity_map)
            action = _resolve_action(sev, on_pii_found)
            findings.append(_finding_to_dict(f, severity=sev, action=action))
            if action == "block":
                blocked = True
                blocked_cats.add(f.category)
        return InputPIICheck(
            findings=findings,
            blocked=blocked,
            blocked_categories=sorted(blocked_cats),
        )

    def mask_for_storage(
        self,
        text: str,
        pii_config: dict[str, Any] | None,
        compliance_mode: str | None = None,
        plain_approved: bool = False,
    ) -> StoragePIIDecision:
        """ADR-020 §4 — chat_logs.question 보관 정책.

        pii.yaml.storage.pii_storage_policy: 'mask' (기본) 또는 'plain'.
        compliance_mode이 'gdpr_strict' 또는 'hipaa_strict'면 mask 강제 (ADR-020 §10).
        plain은 platform_admin 명시 승인(`plain_approved=True`) 시에만 실제 적용된다.
        configured가 plain이지만 승인이 없으면 mask로 fallback하고 plain_approval_missing=True
        를 함께 표기한다 (caller가 chat_log audit 등에 활용).
        """
        cfg = pii_config or {}
        storage_cfg = cfg.get("storage") or {}
        configured = str(storage_cfg.get("pii_storage_policy", "mask")).lower()
        forced = (compliance_mode or "").lower() in {"gdpr_strict", "hipaa_strict"}

        plain_approval_missing = configured == "plain" and not plain_approved
        if plain_approval_missing:
            configured = "mask"

        policy = "mask" if (forced or configured == "mask") else "plain"

        if not text or policy == "plain":
            return StoragePIIDecision(
                policy=policy,
                text=text or "",
                findings=[],
                compliance_forced=forced,
                plain_approval_missing=plain_approval_missing,
            )

        severity_map = cfg.get("severity_map") or {}
        masked_text, raw = self.detector.mask(text)
        findings = [
            _finding_to_dict(
                f,
                severity=_resolve_severity(f.category, f.severity, severity_map),
                action="mask",
            )
            for f in raw
        ]
        return StoragePIIDecision(
            policy=policy,
            text=masked_text,
            findings=findings,
            compliance_forced=forced,
            plain_approval_missing=plain_approval_missing,
        )

    def check_chunk_pii(
        self,
        chunk_content: str,
        pii_config: dict[str, Any] | None,
    ) -> ChunkPIIWarning:
        """ADR-020 §5 (Layer 3) — 인덱싱 시 chunk content PII 검사.

        반환:
          - pii_warnings: chunks.pii_warnings JSONB에 적재할 dict 목록.
            severity_threshold 이상의 finding만 포함 (기본 medium).
          - has_high_severity: high finding이 하나라도 있으면 True
          - block_indexing: indexing.block_indexing=true 이고 high severity가 있으면 True
        """
        cfg = pii_config or {}
        indexing_cfg = cfg.get("indexing") or {}
        if not indexing_cfg.get("enable", True) or not chunk_content:
            return ChunkPIIWarning()

        on_chunk_cfg = indexing_cfg.get("on_pii_found_in_chunk") or {}
        threshold = str(on_chunk_cfg.get("severity_threshold", "medium")).lower()
        threshold_rank = _SEV_RANK.get(threshold, 2)
        block_flag = bool(on_chunk_cfg.get("block_indexing", False))

        severity_map = cfg.get("severity_map") or {}

        raw = self.detector.scan(chunk_content)
        warnings: list[dict[str, Any]] = []
        has_high = False
        for f in raw:
            sev = _resolve_severity(f.category, f.severity, severity_map)
            if _SEV_RANK.get(sev, 0) < threshold_rank:
                continue
            if sev == "high":
                has_high = True
            warnings.append(_finding_to_dict(f, severity=sev, action="warn"))
        return ChunkPIIWarning(
            pii_warnings=warnings,
            has_high_severity=has_high,
            block_indexing=block_flag and has_high,
        )

    def mask_output(
        self, text: str, pii_config: dict[str, Any] | None
    ) -> OutputPIIMask:
        """ADR-020 §6 — 응답 본문 PII 마스킹.

        response.enable=false 또는 빈 입력이면 원문 그대로 반환.
        rule 별 mask_template을 그대로 사용 (pii.yaml의 response.mask_replacement는
        rule mask가 누락된 경우의 fallback로 detector 측에서 이미 처리됨).
        """
        cfg = pii_config or {}
        response_cfg = cfg.get("response") or {}
        if not response_cfg.get("enable", True) or not text:
            return OutputPIIMask(masked_text=text or "", findings=[])

        severity_map = cfg.get("severity_map") or {}
        masked_text, raw = self.detector.mask(text)
        findings = [
            _finding_to_dict(
                f,
                severity=_resolve_severity(f.category, f.severity, severity_map),
                action="mask",
            )
            for f in raw
        ]
        return OutputPIIMask(masked_text=masked_text, findings=findings)
