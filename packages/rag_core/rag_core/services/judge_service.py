"""JudgeService — ADR-010 §4 Inference LLM-as-judge.

`configs/platform/prompts/inference_judge.yaml` + `inference_judge_schema.json`을
로드하여 LLMClient(보통 shared_llm)에 guided_json 호출.

흐름:
  judge(claim, cited_chunks, inference_chain) →
    Jinja2로 system/user 렌더 →
    LLMClient.generate(guided_json_schema=schema) →
    JSON 파싱 → JudgeResult
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined

from ..interfaces.llm_client import LLMClient
from ..interfaces.retriever import RetrievedChunk

_jinja_env = Environment(undefined=StrictUndefined, autoescape=False)


@dataclass(frozen=True)
class JudgePrompt:
    system: str
    user: str
    response_schema: dict[str, Any]
    min_confidence: float = 0.6

    @classmethod
    def load(
        cls,
        *,
        prompt_yaml: Path,
        schema_json: Path,
        min_confidence: float = 0.6,
    ) -> "JudgePrompt":
        with prompt_yaml.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        with schema_json.open("r", encoding="utf-8") as f:
            schema = json.load(f)
        return cls(
            system=str(cfg.get("system", "")),
            user=str(cfg.get("user", "")),
            response_schema=schema,
            min_confidence=min_confidence,
        )


@dataclass
class JudgeResult:
    valid: bool
    confidence: float
    reasoning: str
    caveat: str | None
    raw_response: str
    parse_ok: bool = True
    parse_error: str | None = None

    def passes(self, min_confidence: float) -> bool:
        return self.valid and self.confidence >= min_confidence


class JudgeService:
    """Inference LLM-as-judge.

    Args:
        llm: LLMClient (보통 shared_llm endpoint)
        prompt: JudgePrompt
        model: 호출 모델 이름 (라우팅 결정 결과)
        max_tokens: 응답 최대 토큰
        temperature: 응답 temperature (저온도 권장)
        min_confidence: 통과 임계 — citation.yaml.verification.inference_judge.confidence_threshold
    """

    def __init__(
        self,
        *,
        llm: LLMClient,
        prompt: JudgePrompt,
        model: str,
        max_tokens: int = 512,
        temperature: float = 0.0,
        min_confidence: float | None = None,
    ) -> None:
        self._llm = llm
        self._prompt = prompt
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._min_confidence = (
            min_confidence if min_confidence is not None else prompt.min_confidence
        )

    @property
    def min_confidence(self) -> float:
        return self._min_confidence

    def _render(
        self,
        *,
        claim_text: str,
        cited_chunks: list[RetrievedChunk],
        inference_chain: str | None,
    ) -> str:
        # inference_chain은 inference_judge.yaml user 템플릿이 활용 (있을 때만 블록 렌더).
        system = _jinja_env.from_string(self._prompt.system).render()
        user = _jinja_env.from_string(self._prompt.user).render(
            claim_text=claim_text,
            cited_chunks=[{"content": c.content} for c in cited_chunks],
            inference_chain=inference_chain or "",
        )
        return f"{system}\n\n{user}"

    async def judge(
        self,
        *,
        claim_text: str,
        cited_chunks: list[RetrievedChunk],
        inference_chain: str | None = None,
    ) -> JudgeResult:
        prompt = self._render(
            claim_text=claim_text,
            cited_chunks=cited_chunks,
            inference_chain=inference_chain,
        )
        raw = await self._llm.generate(
            prompt,
            model=self._model,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            guided_json_schema=self._prompt.response_schema,
        )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            return JudgeResult(
                valid=False,
                confidence=0.0,
                reasoning="judge_response_parse_error",
                caveat=None,
                raw_response=raw,
                parse_ok=False,
                parse_error=f"json_decode_error: {e}",
            )
        try:
            return JudgeResult(
                valid=bool(data["valid"]),
                confidence=float(data["confidence"]),
                reasoning=str(data.get("reasoning") or ""),
                caveat=data.get("caveat"),
                raw_response=raw,
            )
        except (KeyError, TypeError, ValueError) as e:
            return JudgeResult(
                valid=False,
                confidence=0.0,
                reasoning="judge_response_schema_violation",
                caveat=None,
                raw_response=raw,
                parse_ok=False,
                parse_error=f"schema_violation: {e}",
            )
