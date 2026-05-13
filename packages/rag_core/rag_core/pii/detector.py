"""
RegexPIIDetector — 정규식 기반 (ADR-020).

룰 source: WiSentinel dlp-core 포팅 + 자체. yaml에서 로드.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from rag_core.interfaces.pii import PIIFinding


@dataclass
class PIIRule:
    category: str
    severity: Literal["low", "medium", "high"]
    pattern: re.Pattern
    mask_template: str  # 마스킹 시 치환 문자열 (예: "***-****-****")


class RegexPIIDetector:
    """ADR-020 §1 — packages/rag_core/rag_core/pii/rules/*.yaml 로드."""

    def __init__(self, rules_dir: str | Path):
        self.rules_dir = Path(rules_dir)
        self.rules: list[PIIRule] = self._load_rules()

    def _load_rules(self) -> list[PIIRule]:
        rules: list[PIIRule] = []
        if not self.rules_dir.exists():
            return rules
        for yml in sorted(self.rules_dir.glob("*.yaml")):
            with yml.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            for rule in data.get("rules", []):
                try:
                    rules.append(
                        PIIRule(
                            category=rule["category"],
                            severity=rule.get("severity", "medium"),
                            pattern=re.compile(rule["pattern"]),
                            mask_template=rule.get("mask", "***"),
                        )
                    )
                except (KeyError, re.error):
                    continue
        return rules

    def scan(self, text: str) -> list[PIIFinding]:
        findings: list[PIIFinding] = []
        for rule in self.rules:
            for m in rule.pattern.finditer(text):
                findings.append(
                    PIIFinding(
                        category=rule.category,
                        severity=rule.severity,
                        position=(m.start(), m.end()),
                        matched_text=m.group(),
                        masked_form=rule.mask_template,
                    )
                )
        return findings

    def mask(self, text: str) -> tuple[str, list[PIIFinding]]:
        findings = self.scan(text)
        # position 역순으로 치환 (인덱스 보존)
        masked = text
        for f in sorted(findings, key=lambda x: -x.position[0]):
            masked = masked[: f.position[0]] + f.masked_form + masked[f.position[1]:]
        return masked, findings
