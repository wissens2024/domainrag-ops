"""AssessmentValidator — ADR-014 §5 LLM-as-judge 4 validator.

각 validator는 동일 pattern: prompt 렌더 → LLM JSON 호출 → {valid, score, reasoning,
suggestions} 응답.

aggregate (ADR-014 §5 + CLAUDE.md Y2):
  - 어느 하나 valid=false 또는 어느 하나 score < 0.5 → quality_status='draft'
  - 그 외(모두 valid=true이고 모든 score >= 0.5) → quality_status='reviewed' (운영자 승인 대기)
  - 자동 approved 전이는 없다. POST /admin/assessment/items/{id}/approve 명시 호출로만 reviewed→approved.

비활성 옵션: config의 validators.{name}.enable=false이면 해당 validator skip.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from rag_core.interfaces.llm_client import LLMClient


_VALIDATOR_NAMES = ("answer", "explanation", "choices", "difficulty")

_VALIDATOR_PROMPTS = {
    "answer": (
        "다음 시험 문제의 정답이 논리적·사실적으로 옳은지 평가하세요.\n"
        "Q: {question}\n선택지: {choices}\n정답: {answer}\n해설: {explanation}\n"
        "JSON 응답: {{\"valid\": bool, \"score\": 0~1, \"reasoning\": str, \"suggestions\": [str]}}"
    ),
    "explanation": (
        "다음 해설이 정답을 정합하게 설명하는지 평가하세요.\n"
        "Q: {question}\n정답: {answer}\n해설: {explanation}\n"
        "JSON 응답: {{\"valid\": bool, \"score\": 0~1, \"reasoning\": str, \"suggestions\": [str]}}"
    ),
    "choices": (
        "다음 객관식 문제의 오답 보기들이 plausible한지(혼동 가능한지) 평가하세요.\n"
        "Q: {question}\n선택지: {choices}\n정답: {answer}\n"
        "JSON 응답: {{\"valid\": bool, \"score\": 0~1, \"reasoning\": str, \"suggestions\": [str]}}"
    ),
    "difficulty": (
        "다음 문제의 난이도 라벨이 실제 난이도와 일치하는지 평가하세요.\n"
        "Q: {question}\n선택지: {choices}\n라벨: {difficulty}\n"
        "JSON 응답: {{\"valid\": bool, \"score\": 0~1, \"reasoning\": str, \"suggestions\": [str]}}"
    ),
}


@dataclass
class ValidatorRunResult:
    name: str
    enabled: bool = True
    valid: bool = True
    score: float = 1.0
    reasoning: str = ""
    suggestions: list[str] = field(default_factory=list)
    raw_response: str | None = None
    parse_error: str | None = None


@dataclass
class ValidationOutcome:
    quality_status: str  # 'draft' | 'reviewed'
    quality_score: float
    validator_results: dict[str, dict[str, Any]] = field(default_factory=dict)


class AssessmentValidator:
    """4 validator를 순차 실행. config로 individual disable 가능."""

    def __init__(self, *, llm_client: LLMClient, model: str = "shared_llm") -> None:
        self._llm = llm_client
        self._model = model

    async def validate(
        self,
        *,
        question_text: str,
        choices: list[Any],
        answer: str,
        explanation: str | None,
        difficulty: str | None,
        validators_config: dict[str, dict[str, Any]] | None = None,
    ) -> ValidationOutcome:
        cfg = validators_config or {}
        results: dict[str, ValidatorRunResult] = {}
        scores: list[float] = []
        any_invalid = False
        any_low = False

        for name in _VALIDATOR_NAMES:
            enabled = bool(cfg.get(name, {}).get("enable", True))
            if not enabled:
                results[name] = ValidatorRunResult(name=name, enabled=False)
                continue
            prompt = _VALIDATOR_PROMPTS[name].format(
                question=question_text,
                choices=", ".join(map(str, choices or [])),
                answer=answer or "",
                explanation=explanation or "",
                difficulty=difficulty or "",
            )
            try:
                raw = await self._llm.generate(
                    prompt,
                    model=self._model,
                    max_tokens=512,
                    temperature=0.0,
                )
            except Exception as exc:  # noqa: BLE001
                results[name] = ValidatorRunResult(
                    name=name, parse_error=f"llm_failed: {exc}",
                    valid=False, score=0.0,
                )
                any_invalid = True
                continue
            parsed, err = _parse_json(raw)
            if err:
                results[name] = ValidatorRunResult(
                    name=name, valid=False, score=0.0,
                    raw_response=raw, parse_error=err,
                )
                any_invalid = True
                continue
            valid = bool(parsed.get("valid", False))
            score = float(parsed.get("score", 0.0) or 0.0)
            results[name] = ValidatorRunResult(
                name=name, valid=valid, score=score,
                reasoning=str(parsed.get("reasoning", "")),
                suggestions=list(parsed.get("suggestions") or []),
                raw_response=raw,
            )
            scores.append(score)
            if not valid:
                any_invalid = True
            if score < 0.5:
                any_low = True

        avg_score = sum(scores) / len(scores) if scores else 1.0

        # Y2 + ADR-014 §5
        if any_invalid or any_low:
            status = "draft"
        else:
            status = "reviewed"  # 운영자 명시 approve 대기

        return ValidationOutcome(
            quality_status=status,
            quality_score=round(avg_score, 4),
            validator_results={
                name: {
                    "enabled": r.enabled, "valid": r.valid, "score": r.score,
                    "reasoning": r.reasoning, "suggestions": r.suggestions,
                    "parse_error": r.parse_error,
                }
                for name, r in results.items()
            },
        )


def _parse_json(raw: str) -> tuple[dict, str | None]:
    if not raw:
        return {}, "empty_response"
    # LLM이 markdown 코드 fence를 두르는 경우 제거
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    try:
        return json.loads(s), None
    except Exception as exc:  # noqa: BLE001
        return {}, f"json_parse_error: {exc}"
