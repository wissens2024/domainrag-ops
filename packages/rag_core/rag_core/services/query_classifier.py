"""QueryClassifier — ADR-013 §3 2-tier 분류.

흐름:
  Tier 1: configs/platform/query_classifier.yaml.tier1 정규식 평가 → 첫 매치 즉시 결정
  Tier 2: 매치 없으면 tier2.enable이 true일 때 LLM(JSON guided)로 분류
          실패·timeout 시 tier2.fallback_decision 반영

ClassificationResult는 chat_logs.classifier_decision에 그대로 저장 가능한 dict.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined

from ..interfaces.llm_client import LLMClient

_jinja_env = Environment(undefined=StrictUndefined, autoescape=False)


@dataclass(frozen=True)
class ClassifierTier2Prompt:
    system: str
    user: str
    response_schema: dict[str, Any]


@dataclass
class ClassificationResult:
    query_type: str
    support_type: str | None = None
    complexity: str | None = None
    tier1_matched: str | None = None
    tier2_called: bool = False
    tier2_parse_ok: bool = True
    tier2_error: str | None = None
    raw_response: str | None = None

    def to_log_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ClassifierConfig:
    """query_classifier.yaml의 런타임 표현."""

    tier1_rules: list[dict[str, Any]] = field(default_factory=list)
    tier2_enable: bool = True
    tier2_timeout_seconds: float = 5.0
    tier2_fallback: dict[str, str] = field(
        default_factory=lambda: {
            "query_type": "document_qa",
            "support_type": "direct",
            "complexity": "medium",
        }
    )

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ClassifierConfig":
        cfg = raw or {}
        tier1 = cfg.get("tier1") or []
        tier2 = cfg.get("tier2") or {}
        return cls(
            tier1_rules=list(tier1),
            tier2_enable=bool(tier2.get("enable", True)),
            tier2_timeout_seconds=float(tier2.get("timeout_seconds", 5.0)),
            tier2_fallback=dict(tier2.get("fallback_decision") or {
                "query_type": "document_qa",
                "support_type": "direct",
                "complexity": "medium",
            }),
        )


def _load_tier2_prompt(path: Path) -> ClassifierTier2Prompt:
    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return ClassifierTier2Prompt(
        system=str(cfg.get("system", "")),
        user=str(cfg.get("user", "")),
        response_schema=dict(cfg.get("response_schema") or {}),
    )


class QueryClassifier:
    """Tier 1 (regex) + Tier 2 (LLM) 분류기.

    Args:
        llm: Tier 2용 LLMClient (보통 tenant_slm — 가벼운 호출)
        prompt: Tier 2 prompt (system/user/schema)
        model: Tier 2 LLM 모델 이름 (라우팅 결과)
    """

    def __init__(
        self,
        *,
        llm: LLMClient | None = None,
        prompt: ClassifierTier2Prompt | None = None,
        model: str | None = None,
    ) -> None:
        self._llm = llm
        self._prompt = prompt
        self._model = model

    @staticmethod
    def load_tier2_prompt(path: Path) -> ClassifierTier2Prompt:
        return _load_tier2_prompt(path)

    @staticmethod
    def tier1_match(question: str, rules: list[dict]) -> dict | None:
        """첫 매치 룰의 dict 반환. 매치 없으면 None."""
        for rule in rules:
            patterns = rule.get("patterns") or []
            for p in patterns:
                try:
                    if re.search(p, question):
                        return rule
                except re.error:
                    continue
        return None

    async def _tier2(
        self,
        question: str,
        config: ClassifierConfig,
    ) -> tuple[dict[str, Any], bool, str | None, str | None]:
        """반환: (decision dict, parse_ok, error, raw)."""
        if self._llm is None or self._prompt is None or self._model is None:
            return dict(config.tier2_fallback), False, "tier2_not_configured", None

        system = _jinja_env.from_string(self._prompt.system).render()
        user = _jinja_env.from_string(self._prompt.user).render(question=question)
        prompt_text = f"{system}\n\n{user}"
        try:
            raw = await self._llm.generate(
                prompt_text,
                model=self._model,
                max_tokens=256,
                temperature=0.0,
                guided_json_schema=self._prompt.response_schema,
            )
        except Exception as e:  # noqa: BLE001
            return dict(config.tier2_fallback), False, f"tier2_call_error: {e}", None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            return dict(config.tier2_fallback), False, f"json_decode_error: {e}", raw

        decision = {
            "query_type": str(data.get("query_type") or config.tier2_fallback.get("query_type", "document_qa")),
            "support_type": data.get("support_type") or config.tier2_fallback.get("support_type"),
            "complexity": data.get("complexity") or config.tier2_fallback.get("complexity"),
        }
        return decision, True, None, raw

    async def classify(
        self,
        *,
        question: str,
        config: ClassifierConfig,
    ) -> ClassificationResult:
        # Tier 1
        match = self.tier1_match(question, config.tier1_rules)
        if match is not None:
            return ClassificationResult(
                query_type=str(match.get("query_type") or "document_qa"),
                support_type=match.get("support_type"),
                complexity=match.get("complexity"),
                tier1_matched=str(match.get("name") or "unnamed"),
                tier2_called=False,
            )
        # Tier 2 (옵션)
        if not config.tier2_enable:
            fb = config.tier2_fallback
            return ClassificationResult(
                query_type=str(fb.get("query_type", "document_qa")),
                support_type=fb.get("support_type"),
                complexity=fb.get("complexity"),
                tier1_matched=None,
                tier2_called=False,
                tier2_parse_ok=False,
                tier2_error="tier2_disabled",
            )
        decision, parse_ok, error, raw = await self._tier2(question, config)
        return ClassificationResult(
            query_type=str(decision.get("query_type") or "document_qa"),
            support_type=decision.get("support_type"),
            complexity=decision.get("complexity"),
            tier1_matched=None,
            tier2_called=True,
            tier2_parse_ok=parse_ok,
            tier2_error=error,
            raw_response=raw,
        )
